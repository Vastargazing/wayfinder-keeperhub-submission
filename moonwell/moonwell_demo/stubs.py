"""Read-only stubs for the services this environment cannot reach.

Rule this file obeys, and the reason it exists as a separate module:
**stubs are only on read/quote paths, never on the send path.** Nothing here
builds, signs, or submits a transaction. Every fund-moving call still goes
`strategy -> real adapter -> encode_call -> send_transaction -> ExternalExecution
-> KeeperHubExecutor -> KeeperHub -> anvil`.

What is stubbed, and why
------------------------
``TOKEN_CLIENT``
    `TokenClient.get_token_details` is `GET {wayfinder api}/blockchain/tokens/detail/`
    with an `X-API-KEY` header and a bare `raise_for_status()` — no offline
    fallback (SOURCE core/clients/TokenClient.py:145-158, WayfinderClient.py).
    We have no key. The strategy reads only `address`, `chain.id`, `decimals`,
    `symbol` and `current_price` from it, so the stub answers exactly those.
    Addresses and decimals are the SDK's own pinned Base constants; prices come
    from LI.FI `/v1/token`, keyless, with a pinned fallback if that is
    unreachable (the fallback is labelled in the returned dict and in evidence).

``TOKEN_CLIENT.get_gas_token``
    Same endpoint family. Only reached through `TokenResolver` for the
    `ethereum-base` id.

``_get_steth_apy``
    `strategy.py:2214-2227` fetches `https://eth-api.lido.fi/v1/protocol/steth/apr/sma`.
    That endpoint is public — no key — and this demo does not need to stub it for
    access reasons. It is stubbed for *determinism* only, and only when
    ``--stub-lido`` is passed; otherwise the real call runs. Either way the value
    feeds only `_quote()` / `_check_quote_profitability()`, which reject solely on
    `apy < 0` (strategy.py:1982-1989), so it never gates a transaction.

Note on token ids (correction to a common assumption)
-----------------------------------------------------
`8453:0x...` does **not** short-circuit `TokenResolver.resolve_token`.
`parse_token_id_to_chain_and_address` splits on ``_``, not ``:``
(SOURCE core/utils/token_refs.py:67-83), so the offline forms are `8453_0x...`,
`base_0x...` or `0x..._base`. The strategy's own ids
(`superbridge-bridged-wsteth-base-base` etc., core/constants/tokens.py:1-7) are
API slugs and cannot short-circuit at all — which is why TOKEN_CLIENT has to be
stubbed rather than side-stepped.
"""

from __future__ import annotations

from typing import Any

from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.utils.token_resolver import TokenResolver

from .lifi import LifiQuoteSource

ZERO = "0x0000000000000000000000000000000000000000"

#: SDK-pinned Base addresses (core/constants/contracts.py). Not invented here.
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_WETH = "0x4200000000000000000000000000000000000006"
BASE_WSTETH = "0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452"
BASE_WELL = "0xA88594D404727625A9437C3f886C7643872296AE"

#: token_id -> (address, decimals, symbol, name, price_proxy_address)
#: `price_proxy_address` is what we ask LI.FI to price; native ETH is priced as WETH.
TOKENS: dict[str, tuple[str, int, str, str, str]] = {
    "usd-coin-base": (BASE_USDC, 6, "USDC", "USD Coin", BASE_USDC),
    "l2-standard-bridged-weth-base-base": (BASE_WETH, 18, "WETH", "Wrapped Ether", BASE_WETH),
    "superbridge-bridged-wsteth-base-base": (BASE_WSTETH, 18, "wstETH", "Wrapped liquid staked Ether 2.0", BASE_WSTETH),
    "ethereum-base": (ZERO, 18, "ETH", "Ethereum", BASE_WETH),
    "moonwell-artemis-base": (BASE_WELL, 18, "WELL", "Moonwell", BASE_WELL),
    "staked-ether-ethereum": (BASE_WSTETH, 18, "stETH", "Liquid staked Ether 2.0", BASE_WSTETH),
}

#: Used only if LI.FI /v1/token is unreachable. Marked in the returned dict.
FALLBACK_PRICE_USD = {
    BASE_USDC: 1.0,
    BASE_WETH: 2457.5,
    BASE_WSTETH: 3054.44,
    BASE_WELL: 0.02,
}

#: 2026-09-06 value of Lido's smaApr/100, used only with --stub-lido.
STUB_STETH_APR = 0.0271


class StubTokenClient:
    """Answers the five fields the Moonwell deposit path reads. Read-only."""

    def __init__(self, lifi: LifiQuoteSource, *, on_evidence=None) -> None:
        self.lifi = lifi
        self.on_evidence = on_evidence
        self._price_cache: dict[str, tuple[float, str]] = {}
        self.calls: list[dict[str, Any]] = []

    def _entry(self, query: str, chain_id: int | None) -> tuple[str, int, str, str, str]:
        q = str(query or "").strip()
        if q in TOKENS:
            return TOKENS[q]
        low = q.lower()
        for addr, dec, sym, name, proxy in TOKENS.values():
            if addr.lower() == low:
                return addr, dec, sym, name, proxy
        # `8453_0x...` / `base_0x...` / `0x..._base` never reach here (the resolver
        # short-circuits them), so anything left is a slug we do not model.
        raise KeyError(
            f"StubTokenClient has no entry for {query!r} (chain_id={chain_id}). "
            "It deliberately models only the tokens this strategy path reads; "
            "add an entry rather than guessing."
        )

    async def _price(self, address: str) -> tuple[float, str]:
        if address in self._price_cache:
            return self._price_cache[address]
        try:
            data = await self.lifi.token(address)
            price = float(data.get("priceUSD") or 0.0)
            if price <= 0:
                raise ValueError(f"LI.FI priced {address} at {price}")
            out = (price, "LI.FI /v1/token (keyless)")
        except Exception as exc:  # noqa: BLE001
            out = (FALLBACK_PRICE_USD.get(address, 0.0), f"PINNED FALLBACK ({exc})")
        self._price_cache[address] = out
        return out

    async def get_token_details(
        self, query: str, market_data: bool = False, chain_id: int | None = None
    ) -> dict[str, Any]:
        address, decimals, symbol, name, proxy = self._entry(query, chain_id)
        out: dict[str, Any] = {
            "token_id": query,
            "address": address,
            "symbol": symbol,
            "name": name,
            "decimals": decimals,
            "chain": {"id": 8453, "code": "base", "name": "Base"},
            "metadata": {"source": "STUB (moonwell_demo.stubs.StubTokenClient)"},
            "_stub": "NOT a Wayfinder TOKEN_CLIENT response",
        }
        if market_data:
            price, source = await self._price(proxy)
            out.update(
                {
                    "current_price": price,
                    "price_change_24h": 0.0,
                    "price_change_percentage_24h": 0.0,
                    "market_cap": 0,
                    "total_volume_usd_24h": 0,
                    "_price_source": source,
                    "_priced_as": proxy,
                }
            )
        rec = {"query": query, "market_data": market_data, "chain_id": chain_id, "result": out}
        self.calls.append(rec)
        if self.on_evidence:
            self.on_evidence("token_stub", rec)
        return out

    async def get_gas_token(self, query: str) -> dict[str, Any]:
        return {
            "id": "ethereum-base",
            "token_id": "ethereum-base",
            "name": "Ethereum",
            "symbol": "ETH",
            "address": ZERO,
            "decimals": 18,
            "chain": {"id": 8453, "code": "base", "name": "Base"},
            "_stub": "NOT a Wayfinder TOKEN_CLIENT response",
        }


class InstalledStubs:
    def __init__(self, token: StubTokenClient, lifi: LifiQuoteSource) -> None:
        self.token = token
        self.lifi = lifi


def install_stubs(*, on_evidence=None, stub_lido: bool = False, strategy=None) -> InstalledStubs:
    """Patch the *singleton instances* the SDK already imported.

    Every consumer does `from ...TokenClient import TOKEN_CLIENT`, a from-import
    that binds the same object into four adapter modules
    (`token_adapter`, `balance_adapter`, `brap_adapter`, `moonwell_adapter`).
    Replacing the module attribute in one place would miss the others; replacing
    the *methods on the shared instance* reaches all of them, including
    `TokenResolver`'s class-level caches, which call `TOKEN_CLIENT` directly.
    """
    lifi = LifiQuoteSource(on_evidence=on_evidence)
    token = StubTokenClient(lifi, on_evidence=on_evidence)

    TOKEN_CLIENT.get_token_details = token.get_token_details  # type: ignore[method-assign]
    TOKEN_CLIENT.get_gas_token = token.get_gas_token  # type: ignore[method-assign]
    BRAP_CLIENT.get_quote = lifi.get_quote  # type: ignore[method-assign]

    # TokenResolver memoises across calls at class level; start clean so a
    # previous process's real answers can never leak into a stubbed run.
    TokenResolver._token_details_cache.clear()
    TokenResolver._gas_token_cache.clear()

    if stub_lido and strategy is not None:
        async def _stub_apr() -> float:
            if on_evidence:
                on_evidence("lido_stub", {"value": STUB_STETH_APR, "source": "STUB constant"})
            return STUB_STETH_APR

        strategy._get_steth_apy = _stub_apr  # type: ignore[method-assign]

    return InstalledStubs(token, lifi)
