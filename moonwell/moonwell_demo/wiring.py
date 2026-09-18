"""Assemble: real strategy -> real seam -> KeeperHubExecutor -> KeeperHub -> fork.

Everything that signs or sends is imported, not written here:

* `wayfinder_paths.strategies.moonwell_wsteth_loop_strategy` (real, unmodified)
* `wayfinder_paths.core.utils.executor.ExternalExecution` / `OperationJournal`
  (the seam, branch `seam/external-executor`, unmodified)
* `keeperhub_executor.KeeperHubExecutor` (research/integration, unmodified)

This module adds three things, none of which is on the decision path:

1. **ABI supply.** KeeperHub's `web3/write-contract` needs an ABI; the Wayfinder
   envelope carries only `to` + `data`. The executor package bundles ERC-20 /
   Aave / WETH9; Moonwell mTokens, the Comptroller and the LiFi Diamond are not
   bundled, so they are registered here. An ABI is only ever a *hypothesis*: the
   executor re-encodes from the JSON-round-tripped args and refuses unless the
   bytes come back identical, so a wrong ABI cannot slip through — it just gets
   refused.
2. **A read-only `eth_call` preflight**, logged as evidence. It never changes
   control flow: refusing here would leave the journal row `pending` and block
   the strategy, which is exactly the failure mode we do not want from a
   diagnostic.
3. **A selector-addressed crash trigger**, so "crash at the borrow step" means
   the borrow and not "the 6th submit".
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
from eth_utils import keccak

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "integration"))
sys.path.insert(0, str(ROOT.parent / "wayfinder" / "upstream"))

from keeperhub_executor import KeeperHubClient, KeeperHubExecutor, register_abi  # noqa: E402
from keeperhub_executor.abis import default_resolver  # noqa: E402
from keeperhub_executor.calldata import find_function_by_selector  # noqa: E402
from keeperhub_executor.reconcile import KeeperHubDbProbe  # noqa: E402
from wayfinder_paths.core.config import CONFIG, get_api_key, set_rpc_urls  # noqa: E402
from wayfinder_paths.core.utils.executor import ExternalExecution, OperationJournal  # noqa: E402
from wayfinder_paths.core.utils.wallets import get_external_executor_callback  # noqa: E402
from wayfinder_paths.strategies.moonwell_wsteth_loop_strategy.strategy import (  # noqa: E402
    MoonwellWstethLoopStrategy,
)

CHAIN_ID = 8453
RPC = os.environ.get("KH_RPC", "http://127.0.0.1:8545")
KH_BASE = os.environ.get("KH_BASE", "http://localhost:3000")
API_KEY_FILE = ROOT.parent / "stand" / "evidence" / ".api-key"

#: The one address KeeperHub's stand can sign for: its `organization_wallets`
#: row, which the DEV/TEST SIGNER patch requires the key to derive to
#: (research/stand/REPORT.md §2a). The seam additionally requires
#: envelope["from"] == executor.wallet_address, so this is also the strategy
#: wallet — and the main wallet, since there is only one signer.
WALLET = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
WSTETH = "0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452"
M_USDC = "0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22"
M_WETH = "0x628ff693426583D9a7FB391E54366292F509D457"
M_WSTETH = "0x627Fe393Bc6EdDA28e99AE648fD6fF362514304b"
COMPTROLLER = "0xfBb21d0380beE3312B33c4353c8936a0F13EF26C"
LIFI_DIAMOND = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"

#: Merged EIP-2535 Diamond ABI for the LiFi router, produced in
#: research/keeperhub/BRAP-CALLDATA.md §2 by reproducing KeeperHub's own
#: fetch-abi Diamond branch (facetAddresses() + per-facet Blockscout fetch +
#: selector-deduplicated merge): 33 facets, 146 functions.
LIFI_DIAMOND_ABI_PATH = (
    ROOT.parent / "keeperhub" / "brap-artifacts" / "abis"
    / "diamond-0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae.json"
)


def _sel(sig: str) -> str:
    return "0x" + keccak(text=sig)[:4].hex()


BORROW_SELECTOR = _sel("borrow(uint256)")  # 0xc5ebeaec

# Hand-written minimal ABIs. Deliberately NOT imported from the SDK's
# moonwell_abi.py: the point of the round trip is that an independently written
# ABI re-encodes the SDK's bytes exactly.
MTOKEN_MIN_ABI: list[dict[str, Any]] = [
    {"type": "function", "name": "mint", "stateMutability": "nonpayable",
     "inputs": [{"name": "mintAmount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "borrow", "stateMutability": "nonpayable",
     "inputs": [{"name": "borrowAmount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "redeem", "stateMutability": "nonpayable",
     "inputs": [{"name": "redeemTokens", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "redeemUnderlying", "stateMutability": "nonpayable",
     "inputs": [{"name": "redeemAmount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "repayBorrow", "stateMutability": "nonpayable",
     "inputs": [{"name": "repayAmount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

COMPTROLLER_MIN_ABI: list[dict[str, Any]] = [
    {"type": "function", "name": "enterMarkets", "stateMutability": "nonpayable",
     "inputs": [{"name": "mTokens", "type": "address[]"}],
     "outputs": [{"name": "", "type": "uint256[]"}]},
    {"type": "function", "name": "exitMarket", "stateMutability": "nonpayable",
     "inputs": [{"name": "mTokenAddress", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "claimReward", "stateMutability": "nonpayable",
     "inputs": [], "outputs": []},
]

ERC20_MIN_ABI: list[dict[str, Any]] = [
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "transfer", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

WETH9_MIN_ABI: list[dict[str, Any]] = ERC20_MIN_ABI + [
    {"type": "function", "name": "deposit", "stateMutability": "payable",
     "inputs": [], "outputs": []},
    {"type": "function", "name": "withdraw", "stateMutability": "nonpayable",
     "inputs": [{"name": "wad", "type": "uint256"}], "outputs": []},
]


_lifi_abi_cache: list[dict[str, Any]] | None = None


def _lifi_abi() -> list[dict[str, Any]]:
    global _lifi_abi_cache
    if _lifi_abi_cache is None:
        _lifi_abi_cache = json.loads(LIFI_DIAMOND_ABI_PATH.read_text())
    return _lifi_abi_cache


def register_demo_abis() -> dict[str, str]:
    """Pin one ABI per contract this loop touches. Returns a provenance map."""
    register_abi(CHAIN_ID, USDC, ERC20_MIN_ABI)
    register_abi(CHAIN_ID, WSTETH, ERC20_MIN_ABI)
    register_abi(CHAIN_ID, WETH, WETH9_MIN_ABI)
    register_abi(CHAIN_ID, M_USDC, MTOKEN_MIN_ABI)
    register_abi(CHAIN_ID, M_WETH, MTOKEN_MIN_ABI)
    register_abi(CHAIN_ID, M_WSTETH, MTOKEN_MIN_ABI)
    register_abi(CHAIN_ID, COMPTROLLER, COMPTROLLER_MIN_ABI)
    return {
        USDC: "hand-written ERC-20",
        WSTETH: "hand-written ERC-20",
        WETH: "hand-written ERC-20 + WETH9",
        M_USDC: "hand-written Compound-v2 mToken",
        M_WETH: "hand-written Compound-v2 mToken",
        M_WSTETH: "hand-written Compound-v2 mToken",
        COMPTROLLER: "hand-written Comptroller (enterMarkets)",
        LIFI_DIAMOND: (
            "EIP-2535 merged Diamond ABI narrowed to the one selector, from "
            "research/keeperhub/brap-artifacts (BRAP-CALLDATA.md §2/§4.2)"
        ),
    }


def demo_abi_resolver(chain_id: int, address: str, selector: str):
    """Registry/bundled first; the LiFi Diamond narrowed to one function last.

    Narrowing matters in practice: the merged Diamond ABI is 418 KB and would be
    stored verbatim in `workflow_executions.input` on every swap. One function
    entry is all `write-contract` needs, and byte equality is unaffected.
    """
    abi = default_resolver(chain_id, address, selector)
    if abi is not None:
        return abi
    if address.lower() == LIFI_DIAMOND.lower():
        fn = find_function_by_selector(_lifi_abi(), selector)
        if fn is not None:
            return [fn]
    return None


def _memoize_fork_block(reconciler) -> None:
    """Ask anvil for its fork config once per process instead of once per poll.

    OBSERVED, twice, on the shared stand: the anvil on :8545 stopped accepting
    connections entirely (listen backlog filling, process alive and idle) while
    a slow write was in flight. In both cases the last requests it logged were
    `anvil_nodeInfo`. `KeeperHubExecutor.lookup` reaches `_block_window` ->
    `ChainReconciler.fork_block()` -> `anvil_nodeInfo` on every poll of a
    non-terminal execution, i.e. once per `poll_interval` for the whole duration
    of a slow step, concurrently with anvil's own `--block-time 1` mining task.

    Caching is unconditionally correct - an anvil instance's fork block never
    changes - and it is applied here, on the instance we own, rather than in the
    executor package, which belongs to another worker. Whether `anvil_nodeInfo`
    is really the trigger is tested separately and independently in
    `scripts/anvil_nodeinfo_probe.py`; this wrapper is a mitigation either way,
    because it removes a per-second RPC call that buys nothing.
    """
    cache: dict[str, Any] = {}
    original = reconciler.fork_block

    async def cached_fork_block():
        if "v" not in cache:
            cache["v"] = await original()
        return cache["v"]

    reconciler.fork_block = cached_fork_block


class DemoKeeperHubExecutor(KeeperHubExecutor):
    """`KeeperHubExecutor` plus a read-only preflight and a selector crash hook.

    Overrides nothing that decides anything: `lookup`, the byte check and the
    submit/idempotency logic are the base class's.
    """

    def __init__(self, *args: Any, selector_crash: Callable[[str, dict], None] | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.selector_crash = selector_crash
        self.selector_counts: dict[str, int] = {}
        self.preflights: list[dict[str, Any]] = []
        _memoize_fork_block(self.reconciler)

    async def _eth_call_preflight(self, envelope: dict[str, Any]) -> dict[str, Any]:
        call = {
            "from": envelope["from"],
            "to": envelope["to"],
            "data": envelope.get("data") or "0x",
            "value": hex(int(envelope.get("value") or 0)),
        }
        out: dict[str, Any] = {"call": call}
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
                body = (
                    await c.post(
                        self.reconciler.rpc_url,
                        json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [call, "latest"]},
                    )
                ).json()
            if "error" in body:
                out["ok"] = False
                out["error"] = body["error"]
            else:
                out["ok"] = True
                out["result"] = body["result"][:200]
        except Exception as exc:  # noqa: BLE001
            out["ok"] = None
            out["error"] = str(exc)
        return out

    async def submit(self, envelope: dict[str, Any], *, operation_id: str) -> str:
        selector = (envelope.get("data") or "0x")[:10]
        self.selector_counts[selector] = self.selector_counts.get(selector, 0) + 1
        pf = await self._eth_call_preflight(envelope)
        pf.update({"operationId": operation_id, "selector": selector,
                   "nth_for_selector": self.selector_counts[selector]})
        self.preflights.append(pf)
        self._emit("preflight", pf)
        if self.selector_crash:
            self.selector_crash(selector, {"operationId": operation_id, "envelope": envelope,
                                           "nth": self.selector_counts[selector]})
        return await super().submit(envelope, operation_id=operation_id)


def assert_offline_sdk() -> None:
    """The two things this whole demo claims about the SDK process."""
    assert get_api_key() is None, "must run without a Wayfinder API key"
    assert not CONFIG.get("wallets"), "must run with no local wallets / private keys"


def build(
    *,
    workflow_id: str,
    state_dir: Path,
    on_evidence=None,
    crash_hook=None,
    selector_crash=None,
    submit_timeout: float = 180.0,
    db_probe: bool = True,
):
    """Return (strategy, execution, executor, client, journal)."""
    set_rpc_urls({str(CHAIN_ID): RPC})
    assert_offline_sdk()
    register_demo_abis()

    client = KeeperHubClient(KH_BASE, API_KEY_FILE.read_text().strip())
    executor = DemoKeeperHubExecutor(
        execution_profile="direct",
        client=client,
        workflow_id=workflow_id,
        wallet_address=WALLET,
        chain_id=CHAIN_ID,
        rpc_url=RPC,
        abi_resolver=demo_abi_resolver,
        db_probe=KeeperHubDbProbe() if db_probe else None,
        submit_timeout=submit_timeout,
        poll_interval=3.0,  # see _memoize_fork_block: be gentle with the shared anvil
        crash_hook=crash_hook,
        on_evidence=on_evidence,
        selector_crash=selector_crash,
    )
    journal = OperationJournal(state_dir / "sdk-journal.sqlite")
    execution = ExternalExecution(executor, journal)
    sign_callback = get_external_executor_callback(execution)

    strategy = MoonwellWstethLoopStrategy(
        {
            "main_wallet": {"address": WALLET},
            "strategy_wallet": {"address": WALLET},
        },
        main_wallet_signing_callback=sign_callback,
        strategy_wallet_signing_callback=sign_callback,
    )
    return strategy, execution, executor, client, journal
