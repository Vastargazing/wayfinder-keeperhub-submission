"""LI.FI as a **STAND-IN for BRAP** on the WETH -> wstETH swap leg.

Why a stand-in at all
---------------------
`BRAPAdapter.best_quote` calls `BRAP_CLIENT.get_quote`, which is
`GET {wayfinder api}/blockchain/braps/quote/` with an `X-API-KEY` header
(SOURCE `core/clients/BRAPClient.py:76-121`). We have no Wayfinder API key, and
the endpoint has no unauthenticated path: `research/keeperhub/BRAP-CALLDATA.md`
§1.2 recorded HTTP 401 for exactly this call. So the quote must come from
somewhere else, and the honest thing is to say precisely what changes.

What is the same as a real BRAP quote
-------------------------------------
* The consumer is unchanged. `BRAPAdapter.best_quote` and
  `BRAPAdapter.swap_from_quote` are the SDK's real methods; `swap_from_quote`
  reads only `quote["calldata"] = {data, to, value, chainId}` and
  `quote["input_amount"]` (SOURCE brap_adapter/adapter.py:153-223). We return
  exactly that shape, so the send path is byte-for-byte the SDK's.
* The calldata is **real router calldata** produced by a live aggregator for
  this exact leg, not synthesised by us. `research/keeperhub/BRAP-CALLDATA.md`
  §3 established that a LI.FI quote for 0.5 WETH -> wstETH on Base round-trips
  byte-for-byte through KeeperHub's ABI + functionName + args pipeline.
* The approval, the send, the seam, the executor and KeeperHub are untouched.

What differs, precisely
-----------------------
1. **Who returns the calldata.** Real: Wayfinder's BRAP service, aggregating
   providers it chooses, keyed by our account. Here: `GET https://li.quest/v1/quote`,
   keyless, LI.FI's own aggregation.
2. **Which router.** Real: unknown to us; the only provider named anywhere in the
   SDK is `"enso"` (BRAP-CALLDATA §1.2), whose `EnsoShortcutRouter` also
   round-trips (case M). Here: the LiFi Diamond
   `0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE`, function
   `swapTokensMultipleV3ERC20ToERC20`, selector `0x5fd9ae2e`, currently routed
   by LI.FI through KyberSwap.
   **If a real BRAP quote came back for Odos `swapCompact()`, 1inch V6, or 0x
   AllowanceHolder, this whole path would refuse** rather than execute — those
   three are not canonical ABI encoding and KeeperHub's write-contract node
   cannot reproduce them (BRAP-CALLDATA §4.1). That refusal is the correct
   behaviour, and it is the single biggest thing a real BRAP key would change.
3. **Provider preference is ignored.** The strategy asks for
   `preferred_providers=["aerodrome", "enso"]` (strategy.py:2427). BRAP would
   filter its own quote list by that. We return one quote whose `provider` is
   `"lifi"`, so `_select_quote_by_provider` finds no match and
   `best_quote` falls through to `best_quote`/our single entry
   (SOURCE brap_adapter/adapter.py:131-139). Same outcome, different reason.
4. **Fields BRAP carries that we do not populate meaningfully**:
   `wrap_transaction` / `unwrap_transaction` (None — the loop wraps via
   `MoonwellAdapter.wrap_eth`, not via the quote), `safety_warnings`,
   `output_validation`, `route`, `fee_estimate` (fee data is copied from LI.FI
   where it exists). None of them is read on the send path.

Slippage
--------
The strategy asks for 0.5%..3%. Against a fork whose state is hours behind live
Base, a live quote's `minAmountOut` is computed from live pool state, so a tight
bound can revert on the fork for reasons that have nothing to do with the
integration. `MIN_FORK_SLIPPAGE` raises the floor and every quote records both
the requested and the effective value.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

LIFI_QUOTE_URL = "https://li.quest/v1/quote"
LIFI_TOKEN_URL = "https://li.quest/v1/token"
INTEGRATOR = "wf-kh-demo"  # LI.FI caps this at 23 chars

#: Floor applied to the strategy's slippage because the fork lags live Base.
#: Recorded in the evidence for every quote; see the module docstring.
MIN_FORK_SLIPPAGE = 0.05

PROVIDER_LABEL = "lifi"


class QuoteUnavailable(RuntimeError):
    """LI.FI could not price this leg. Surfaces to the SDK as a quote failure."""


class LifiQuoteSource:
    """Keyless quote source shaped like `BRAPClient.get_quote`.

    Instances are installed onto the SDK's `BRAP_CLIENT` singleton by
    :func:`moonwell_demo.stubs.install_stubs`; nothing here signs or sends.
    """

    def __init__(self, *, on_evidence=None, timeout: float = 90.0) -> None:
        self.on_evidence = on_evidence
        self.timeout = timeout
        self.calls: list[dict[str, Any]] = []

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as c:
            resp = await c.get(url, params=params)
        if resp.status_code != 200:
            raise QuoteUnavailable(f"{url} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def token(self, address: str, chain_id: int = 8453) -> dict[str, Any]:
        return await self._get(LIFI_TOKEN_URL, {"chain": chain_id, "token": address})

    # -- the BRAPClient.get_quote signature -------------------------------

    async def get_quote(
        self,
        *,
        from_token: str,
        to_token: str,
        from_chain: int,
        to_chain: int,
        from_wallet: str,
        from_amount: str,
        slippage: float | None = None,
        to_wallet: str | None = None,
        allow_unverified_output: bool = False,
    ) -> dict[str, Any]:
        if int(from_chain) != int(to_chain):
            raise QuoteUnavailable(
                "this stand-in is same-chain only; a cross-chain BRAP quote is not modelled"
            )
        requested = float(slippage) if slippage is not None else 0.005
        effective = max(requested, MIN_FORK_SLIPPAGE)
        params = {
            "fromChain": int(from_chain),
            "toChain": int(to_chain),
            "fromToken": from_token,
            "toToken": to_token,
            "fromAddress": from_wallet,
            "toAddress": to_wallet or from_wallet,
            "fromAmount": str(from_amount),
            "slippage": effective,
            "integrator": INTEGRATOR,
        }
        started = time.time()
        raw = await self._get(LIFI_QUOTE_URL, params)
        tx = raw.get("transactionRequest") or {}
        if not tx.get("data") or not tx.get("to"):
            raise QuoteUnavailable("LI.FI returned no transactionRequest")

        est = raw.get("estimate") or {}
        entry = self._to_brap_entry(raw, tx, est, params)
        record = {
            "source": "LI.FI /v1/quote (STAND-IN for BRAP)",
            "requested_slippage": requested,
            "effective_slippage": effective,
            "min_fork_slippage": MIN_FORK_SLIPPAGE,
            "params": params,
            "tool": raw.get("tool"),
            "toolDetails": raw.get("toolDetails"),
            "router": tx.get("to"),
            "approvalAddress": est.get("approvalAddress"),
            "selector": tx["data"][:10],
            "calldata_len_bytes": len(tx["data"]) // 2 - 1,
            "input_amount": entry["input_amount"],
            "output_amount": entry["output_amount"],
            "min_amount_out": est.get("toAmountMin"),
            "elapsed_s": round(time.time() - started, 3),
            "raw_quote": raw,
        }
        self.calls.append(record)
        if self.on_evidence:
            self.on_evidence("brap_stand_in_quote", record)
        return {"quotes": [entry], "best_quote": entry, "errors": []}

    @staticmethod
    def _to_brap_entry(
        raw: dict[str, Any], tx: dict[str, Any], est: dict[str, Any], params: dict[str, Any]
    ) -> dict[str, Any]:
        """LI.FI response -> the `BRAPQuoteEntry` fields the SDK actually reads.

        `swap_from_quote` reads `calldata` and `input_amount`; `_swap_with_retries`
        reads `to_amount` off the *result*, which `swap_from_quote` fills from
        `output_amount`. Everything else is carried for evidence only.
        """
        value = tx.get("value") or "0x0"
        calldata = {
            "data": tx["data"],
            "to": tx["to"],
            "value": str(int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)),
            "chainId": int(params["fromChain"]),
        }
        return {
            "provider": PROVIDER_LABEL,
            "quote": {
                "gas": str(est.get("gasCosts", [{}])[0].get("estimate", "0")),
                "amountOut": str(est.get("toAmount", "0")),
                "priceImpact": 0,
                "feeAmount": [],
                "minAmountOut": str(est.get("toAmountMin", "0")),
                "createdAt": int(time.time()),
                "tx": dict(calldata),
                "route": [],
            },
            "calldata": calldata,
            "input_amount": int(est.get("fromAmount", params["fromAmount"])),
            "output_amount": int(est.get("toAmount", 0)),
            "gas_estimate": int(est.get("gasCosts", [{}])[0].get("estimate", 0) or 0),
            "input_amount_usd": float(raw.get("action", {}).get("fromToken", {}).get("priceUSD", 0) or 0)
            * int(est.get("fromAmount", 0))
            / 10 ** int(raw.get("action", {}).get("fromToken", {}).get("decimals", 18)),
            "output_amount_usd": float(raw.get("action", {}).get("toToken", {}).get("priceUSD", 0) or 0)
            * int(est.get("toAmount", 0))
            / 10 ** int(raw.get("action", {}).get("toToken", {}).get("decimals", 18)),
            "fee_estimate": {"fee_total_usd": 0.0, "fee_breakdown": []},
            "wrap_transaction": None,
            "unwrap_transaction": None,
            "native_input": False,
            "native_output": False,
            "safety_warnings": None,
            "output_validation": None,
            # Not a BRAP field. Present so an evidence reader can never mistake
            # this entry for a real BRAP response.
            "_stand_in": "LI.FI /v1/quote; NOT a Wayfinder BRAP quote",
        }
