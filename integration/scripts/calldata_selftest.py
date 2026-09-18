"""Byte-equality self-test for the calldata bridge. Writes evidence/60-calldata-selftest.json.

Two halves:

* every calldata shape the Aave path actually produces, plus a tuple-bearing
  synthetic case, must round-trip byte-for-byte;
* every non-canonical shape must be REFUSED, not silently repaired.

The tuple case is here because `functionArgs` for a struct argument is the shape
BRAP-CALLDATA.md §3 proved KeeperHub accepts (name-keyed objects), and this
package must emit exactly that. It is a local round trip only — no tuple call
was submitted through KeeperHub in these runs (README §6).
"""

from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eth_abi import encode as abi_encode  # noqa: E402

from keeperhub_executor.abis import AAVE_V3_POOL_ABI, ERC20_ABI, default_resolver  # noqa: E402
from keeperhub_executor.calldata import (  # noqa: E402
    CalldataReproductionError,
    canonical_type,
    decode_and_verify,
    selector_of,
)
from keeperhub_executor.executor import wei_to_ether_string  # noqa: E402

WALLET = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
MAX = 2**256 - 1

TUPLE_ABI = [
    {
        "type": "function",
        "name": "exactInputSingle",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    }
]


def build(abi: list[dict], name: str, values: list) -> str:
    fn = next(e for e in abi if e.get("name") == name)
    types = [canonical_type(i) for i in fn["inputs"]]
    return selector_of(fn) + abi_encode(types, values).hex()


def main() -> None:
    accept: dict[str, tuple[str, list[dict]]] = {
        "erc20.approve(pool, 100 USDC)": (
            build(ERC20_ABI, "approve", [POOL, 100_000_000]),
            ERC20_ABI,
        ),
        "erc20.approve(pool, MAX)  # what AaveV3Adapter.lend actually emits": (
            build(ERC20_ABI, "approve", [POOL, MAX]),
            ERC20_ABI,
        ),
        "aave.supply(USDC, 100e6, wallet, 0)": (
            build(AAVE_V3_POOL_ABI, "supply", [USDC, 100_000_000, WALLET, 0]),
            AAVE_V3_POOL_ABI,
        ),
        "aave.withdraw(USDC, MAX, wallet)": (
            build(AAVE_V3_POOL_ABI, "withdraw", [USDC, MAX, WALLET]),
            AAVE_V3_POOL_ABI,
        ),
        "aave.setUserUseReserveAsCollateral(USDC, false)  # bool": (
            build(AAVE_V3_POOL_ABI, "setUserUseReserveAsCollateral", [USDC, False]),
            AAVE_V3_POOL_ABI,
        ),
        "uniswap.exactInputSingle((...))  # 7-member struct, uint24/uint160": (
            build(
                TUPLE_ABI,
                "exactInputSingle",
                [(USDC, POOL, 500, WALLET, 10**6, 1, 0)],
            ),
            TUPLE_ABI,
        ),
    }

    canonical = build(ERC20_ABI, "approve", [POOL, 100_000_000])
    refuse: dict[str, tuple[str, list[dict]]] = {
        "non-canonical address padding": (
            canonical[:10] + "00000000000000000000dead" + canonical[34:],
            ERC20_ABI,
        ),
        "4 trailing bytes (1inch V6 shape)": (canonical + "7f653840", ERC20_ABI),
        "truncated tail": (canonical[:-8], ERC20_ABI),
        "selector not in ABI (Odos swapCompact)": (
            "0x83bd37f9" + canonical[10:],
            ERC20_ABI,
        ),
        "selector only, no args (Odos swapCompact literal)": ("0x83bd37f9", ERC20_ABI),
        "no selector": ("0x", ERC20_ABI),
    }

    out: dict = {"accept": [], "refuse": [], "ethValue": []}
    ok = True

    for label, (data, abi) in accept.items():
        try:
            d = decode_and_verify(data, abi)
            entry = {"case": label, "result": "EQUAL", **d.as_evidence()}
            entry["functionArgs"] = d.function_args_json()
            if not entry["byte_equal"]:
                ok = False
                entry["result"] = "NOT_EQUAL"
        except CalldataReproductionError as exc:
            ok = False
            entry = {"case": label, "result": "UNEXPECTED_REFUSAL", **exc.as_dict()}
        out["accept"].append(entry)
        print(f"[accept] {entry['result']:18} {label}")

    for label, (data, abi) in refuse.items():
        try:
            decode_and_verify(data, abi)
            ok = False
            entry = {"case": label, "result": "WRONGLY_ACCEPTED"}
        except CalldataReproductionError as exc:
            entry = {"case": label, "result": "REFUSED", "error": str(exc)[:220]}
        out["refuse"].append(entry)
        print(f"[refuse] {entry['result']:18} {label}")

    # The default_resolver must find the ABI for the two selectors the Aave path
    # uses, and must find nothing for a selector no bundled ABI declares.
    out["resolver"] = {
        "0x095ea7b3": default_resolver(8453, USDC, "0x095ea7b3") is not None,
        "0x617ba037": default_resolver(8453, POOL, "0x617ba037") is not None,
        "0x83bd37f9": default_resolver(8453, USDC, "0x83bd37f9") is not None,
    }
    if not (out["resolver"]["0x095ea7b3"] and out["resolver"]["0x617ba037"]) or out[
        "resolver"
    ]["0x83bd37f9"]:
        ok = False

    for wei in (0, 1, 10**18, 10**18 + 1, 12345678901234567890, 2**256 - 1):
        s = wei_to_ether_string(wei)
        out["ethValue"].append({"wei": str(wei), "ethValue": s})
        print(f"[ethValue] {wei} -> {s!r}")

    out["all_passed"] = ok
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, default=ROOT / "evidence" / "revision-2" / "60-calldata-selftest.json")
    dest = ap.parse_args().output
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(out, indent=1)
    )
    print(f"\nall_passed = {ok}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
