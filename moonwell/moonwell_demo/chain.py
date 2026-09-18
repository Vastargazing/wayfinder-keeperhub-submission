"""Direct chain reads for evidence, independent of the SDK and of KeeperHub.

Everything here is `eth_call` / `eth_get*` straight at the fork's RPC. It exists
so that no claim in the evidence rests on the SDK's bookkeeping or on KeeperHub's
records: "the position after step N" is read from the chain at the block that
step landed in, and "the calldata KeeperHub broadcast" is read back out of
`eth_getTransactionByHash(...).input`.
"""

from __future__ import annotations

from typing import Any

import httpx
from eth_utils import keccak

from .wiring import (
    COMPTROLLER,
    M_USDC,
    M_WETH,
    M_WSTETH,
    RPC,
    USDC,
    WETH,
    WSTETH,
)


def _sel(sig: str) -> str:
    return "0x" + keccak(text=sig)[:4].hex()


SEL_BALANCE_OF = _sel("balanceOf(address)")
SEL_ACCOUNT_LIQUIDITY = _sel("getAccountLiquidity(address)")
SEL_BORROW_BALANCE_STORED = _sel("borrowBalanceStored(address)")
SEL_EXCHANGE_RATE_STORED = _sel("exchangeRateStored()")


def _addr_arg(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


class Chain:
    def __init__(self, rpc_url: str = RPC, *, timeout: float = 60.0) -> None:
        self.rpc_url = rpc_url
        self.timeout = timeout
        self.errors: list[dict[str, Any]] = []

    async def rpc(self, method: str, params: list[Any]) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as c:
            body = (
                await c.post(
                    self.rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
            ).json()
        if "error" in body:
            raise RuntimeError(f"{method}: {body['error']}")
        return body["result"]

    async def call(self, to: str, data: str, block: Any = "latest") -> str:
        return await self.rpc("eth_call", [{"to": to, "data": data}, block])

    async def _uint(self, to: str, data: str, block: Any = "latest") -> int | None:
        """None on a failed read, never a silent 0.

        Historical reads against an anvil fork can fail with
        ``missing bytecode for code hash ...`` (the fork backend cannot serve the
        archive state). Returning 0 there would fabricate a position; None says
        "not read", and the caller records the error alongside.
        """
        try:
            raw = await self.call(to, data, block)
        except Exception as exc:  # noqa: BLE001
            self.errors.append({"to": to, "selector": data[:10], "block": str(block), "error": str(exc)})
            return None
        return int(raw, 16) if raw and raw != "0x" else 0

    async def erc20_balance(self, token: str, who: str, block: Any = "latest") -> int | None:
        return await self._uint(token, SEL_BALANCE_OF + _addr_arg(who), block)

    async def eth_balance(self, who: str, block: Any = "latest") -> int | None:
        try:
            return int(await self.rpc("eth_getBalance", [who, block]), 16)
        except Exception as exc:  # noqa: BLE001
            self.errors.append({"call": "eth_getBalance", "block": str(block), "error": str(exc)})
            return None

    async def nonce(self, who: str, block: Any = "latest") -> int | None:
        try:
            return int(await self.rpc("eth_getTransactionCount", [who, block]), 16)
        except Exception as exc:  # noqa: BLE001
            self.errors.append({"call": "eth_getTransactionCount", "block": str(block), "error": str(exc)})
            return None

    async def account_liquidity(self, who: str, block: Any = "latest") -> dict[str, Any]:
        try:
            raw = await self.call(COMPTROLLER, SEL_ACCOUNT_LIQUIDITY + _addr_arg(who), block)
        except Exception as exc:  # noqa: BLE001
            self.errors.append({"call": "getAccountLiquidity", "block": str(block), "error": str(exc)})
            return {"error": None, "liquidity_1e18_usd": None, "shortfall_1e18_usd": None, "read_error": str(exc)}
        body = raw[2:]
        return {
            "error": int(body[0:64], 16),
            "liquidity_1e18_usd": int(body[64:128], 16),
            "shortfall_1e18_usd": int(body[128:192], 16),
        }

    async def borrow_balance(self, mtoken: str, who: str, block: Any = "latest") -> int | None:
        return await self._uint(mtoken, SEL_BORROW_BALANCE_STORED + _addr_arg(who), block)

    async def position(self, who: str, block: Any = "latest") -> dict[str, Any]:
        """The whole Moonwell position plus wallet balances, at one block."""
        before = len(self.errors)
        liq = await self.account_liquidity(who, block)
        out = {
            "block": block,
            "wallet": {
                "eth_wei": await self.eth_balance(who, block),
                "usdc": await self.erc20_balance(USDC, who, block),
                "weth": await self.erc20_balance(WETH, who, block),
                "wsteth": await self.erc20_balance(WSTETH, who, block),
            },
            "moonwell": {
                "mUSDC": await self.erc20_balance(M_USDC, who, block),
                "mwstETH": await self.erc20_balance(M_WSTETH, who, block),
                "mWETH": await self.erc20_balance(M_WETH, who, block),
                "weth_debt_wei": await self.borrow_balance(M_WETH, who, block),
                "account_liquidity": liq,
            },
            "nonce": await self.nonce(who, block),
        }
        new_errors = self.errors[before:]
        if new_errors:
            out["read_errors"] = new_errors
        return out

    async def transaction(self, txn_hash: str) -> dict[str, Any] | None:
        return await self.rpc("eth_getTransactionByHash", [txn_hash])

    async def receipt(self, txn_hash: str) -> dict[str, Any] | None:
        return await self.rpc("eth_getTransactionReceipt", [txn_hash])

    async def block_number(self) -> int:
        return int(await self.rpc("eth_blockNumber", []), 16)

    _fork_block_cache: dict[str, int | None] = {}

    async def fork_block(self) -> int | None:
        """Memoised: `anvil_nodeInfo` is called once per process, not per read.

        See `moonwell_demo.wiring._memoize_fork_block` for why that matters on
        this shared anvil.
        """
        if self.rpc_url in Chain._fork_block_cache:
            return Chain._fork_block_cache[self.rpc_url]
        try:
            info = await self.rpc("anvil_nodeInfo", [])
            value = int(info["forkConfig"]["forkBlockNumber"])
        except Exception:  # noqa: BLE001
            value = None
        Chain._fork_block_cache[self.rpc_url] = value
        return value

    async def onchain_calldata_matches(self, txn_hash: str, envelope: dict[str, Any]) -> dict[str, Any]:
        """The check that closes the loop: what landed vs what the SDK authorized."""
        tx = await self.transaction(txn_hash)
        rec = await self.receipt(txn_hash)
        if tx is None:
            return {"hash": txn_hash, "found": False}
        obs_data = str(tx.get("input") or "0x").lower()
        exp_data = str(envelope.get("data") or "0x").lower()
        return {
            "hash": txn_hash,
            "found": True,
            "from_matches": str(tx.get("from", "")).lower() == str(envelope["from"]).lower(),
            "to_matches": str(tx.get("to") or "").lower() == str(envelope.get("to") or "").lower(),
            "value_matches": int(tx.get("value", "0x0"), 16) == int(envelope.get("value") or 0),
            "calldata_byte_equal": obs_data == exp_data,
            "calldata_len_bytes": len(obs_data) // 2 - 1,
            "onchain_input": obs_data,
            "envelope_data": exp_data,
            "blockNumber": int(tx["blockNumber"], 16) if tx.get("blockNumber") else None,
            "nonce": int(tx["nonce"], 16) if tx.get("nonce") else None,
            "receiptStatus": int(rec["status"], 16) if rec and rec.get("status") else None,
            "gasUsed": int(rec["gasUsed"], 16) if rec and rec.get("gasUsed") else None,
        }
