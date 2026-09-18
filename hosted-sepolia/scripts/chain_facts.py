#!/usr/bin/env python3
"""Independently re-verify the Base Sepolia on-chain facts this task starts from.

Reads only. No key material, no signing, no writes. Uses public RPC endpoints
directly so the result does not depend on any KeeperHub or Wayfinder config.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

RPCS = [
    "https://sepolia.base.org",
    "https://base-sepolia-rpc.publicnode.com",
]
CHAIN_ID = 84532

POOL = "0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27"
USDC_AAVE = "0xba50Cd2A20f6DA35D788639E581bca8d0B5d4D5f"
AUSDC = "0x10F1A9D11CDf50041f3f8cB7191CBE2f31750ACC"
FAUCET = "0xD9145b5F45Ad4519c7ACcD6E0A4A82e83bB8A6Dc"
USDC_CIRCLE = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
WALLET = "0x7BEa5c59c12aebF9343087EF1513f651678291F5"

_id = [0]


def rpc(method: str, params: list[Any], url: str = RPCS[0]) -> Any:
    _id[0] += 1
    with httpx.Client(timeout=30, trust_env=False) as c:
        r = c.post(url, json={"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
    if "error" in body:
        raise RuntimeError(f"{method} {params}: {body['error']}")
    return body["result"]


def call(to: str, data: str, url: str = RPCS[0]) -> str:
    return rpc("eth_call", [{"to": to, "data": data}, "latest"], url)


def sel(sig: str) -> str:
    from eth_utils import function_signature_to_4byte_selector

    return "0x" + function_signature_to_4byte_selector(sig).hex()


def addr_arg(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def uint_arg(n: int) -> str:
    return f"{n:064x}"


def dec_addr(word: str) -> str:
    from eth_utils import to_checksum_address

    return to_checksum_address("0x" + word[-40:])


def dec_str(hexdata: str) -> str:
    raw = bytes.fromhex(hexdata[2:])
    if len(raw) < 64:
        return raw.decode("utf-8", "replace").rstrip("\x00")
    length = int.from_bytes(raw[32:64], "big")
    return raw[64 : 64 + length].decode("utf-8", "replace")


def main() -> int:
    out: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rpcs": RPCS,
        "chain_id_expected": CHAIN_ID,
    }

    out["chain_id_by_rpc"] = {}
    for u in RPCS:
        try:
            out["chain_id_by_rpc"][u] = int(rpc("eth_chainId", [], u), 16)
        except Exception as exc:  # noqa: BLE001
            out["chain_id_by_rpc"][u] = f"ERROR: {exc}"

    out["block_number"] = int(rpc("eth_blockNumber", []), 16)

    # 1. bytecode presence
    code = {}
    for name, a in [
        ("pool", POOL),
        ("usdc_aave", USDC_AAVE),
        ("aUSDC", AUSDC),
        ("faucet", FAUCET),
        ("usdc_circle", USDC_CIRCLE),
        ("turnkey_wallet", WALLET),
    ]:
        c = rpc("eth_getCode", [a, "latest"])
        code[name] = {"address": a, "code_len_bytes": (len(c) - 2) // 2, "is_contract": len(c) > 2}
    out["bytecode"] = code

    # 2. ERC-20 metadata
    tokens = {}
    for name, a in [("usdc_aave", USDC_AAVE), ("aUSDC", AUSDC), ("usdc_circle", USDC_CIRCLE)]:
        tokens[name] = {
            "address": a,
            "symbol": dec_str(call(a, sel("symbol()"))),
            "name": dec_str(call(a, sel("name()"))),
            "decimals": int(call(a, sel("decimals()")), 16),
            "totalSupply": int(call(a, sel("totalSupply()")), 16),
        }
    out["tokens"] = tokens

    # 3. Aave reserve config: getReserveData(address) -> struct; aTokenAddress is word 8
    rd = call(POOL, sel("getReserveData(address)") + addr_arg(USDC_AAVE))
    raw = bytes.fromhex(rd[2:])
    words = [raw[i : i + 32].hex() for i in range(0, len(raw), 32)]
    out["reserve_data_words"] = len(words)
    # Aave v3 ReserveData layout (v3.1/3.2): 0 configuration, 1 liquidityIndex+... packed
    # Rather than guess offsets, scan every word that decodes to a known address.
    candidates = {}
    for i, w in enumerate(words):
        if w[:24] == "0" * 24 and int(w, 16) != 0:
            candidates[i] = dec_addr(w)
    out["reserve_address_words"] = candidates
    out["aToken_matches_expected"] = AUSDC.lower() in {v.lower() for v in candidates.values()}

    # aToken.UNDERLYING_ASSET_ADDRESS() must point back at the Aave test USDC
    try:
        ua = dec_addr(call(AUSDC, sel("UNDERLYING_ASSET_ADDRESS()"))[2:])
        out["aToken_underlying"] = ua
        out["aToken_underlying_matches"] = ua.lower() == USDC_AAVE.lower()
    except Exception as exc:  # noqa: BLE001
        out["aToken_underlying"] = f"ERROR: {exc}"

    try:
        pool_of_atoken = dec_addr(call(AUSDC, sel("POOL()"))[2:])
        out["aToken_pool"] = pool_of_atoken
        out["aToken_pool_matches"] = pool_of_atoken.lower() == POOL.lower()
    except Exception as exc:  # noqa: BLE001
        out["aToken_pool"] = f"ERROR: {exc}"

    # Circle USDC must NOT be a configured Aave reserve
    try:
        rd_circle = call(POOL, sel("getReserveData(address)") + addr_arg(USDC_CIRCLE))
        raw_c = bytes.fromhex(rd_circle[2:])
        words_c = [raw_c[i : i + 32].hex() for i in range(0, len(raw_c), 32)]
        nonzero = [i for i, w in enumerate(words_c) if int(w, 16) != 0]
        out["circle_usdc_reserve_nonzero_words"] = nonzero
        # A nonzero ReserveData word is not reserve membership (word11 was a
        # false positive in historical evidence/01). Use the canonical list.
        from eth_abi import decode
        listed = decode(["address[]"], bytes.fromhex(call(POOL, sel("getReservesList()"))[2:]))[0]
        out["circle_usdc_is_configured_reserve"] = USDC_CIRCLE.lower() in {a.lower() for a in listed}
        out["circle_usdc_membership_source"] = "Pool.getReservesList()"
    except Exception as exc:  # noqa: BLE001
        out["circle_usdc_reserve"] = f"ERROR: {exc}"

    # 4. Faucet properties
    faucet = {}
    try:
        faucet["isPermissioned"] = int(call(FAUCET, sel("isPermissioned()")), 16) == 1
    except Exception as exc:  # noqa: BLE001
        faucet["isPermissioned"] = f"ERROR: {exc}"
    try:
        faucet["owner"] = dec_addr(call(FAUCET, sel("owner()"))[2:])
    except Exception as exc:  # noqa: BLE001
        faucet["owner"] = f"ERROR: {exc}"
    for fn, key in [
        ("getMaximumMintAmount(address)", "maximumMintAmount_usdc"),
        ("getMintLimit(address)", "mintLimit_usdc"),
    ]:
        try:
            faucet[key] = int(call(FAUCET, sel(fn) + addr_arg(USDC_AAVE)), 16)
        except Exception as exc:  # noqa: BLE001
            faucet[key] = f"ERROR: {exc}"
    for fn, key in [("MINT_INTERVAL()", "mint_interval"), ("mintInterval()", "mintInterval")]:
        try:
            faucet[key] = int(call(FAUCET, sel(fn)), 16)
        except Exception as exc:  # noqa: BLE001
            faucet[key] = f"ERROR: {exc}"
    out["faucet"] = faucet

    # 5. Wallet state
    w: dict[str, Any] = {"address": WALLET}
    w["nonce"] = int(rpc("eth_getTransactionCount", [WALLET, "latest"]), 16)
    w["eth_balance_wei"] = int(rpc("eth_getBalance", [WALLET, "latest"]), 16)
    for name, a in [("usdc_aave", USDC_AAVE), ("aUSDC", AUSDC), ("usdc_circle", USDC_CIRCLE)]:
        w[f"balance_{name}"] = int(call(a, sel("balanceOf(address)") + addr_arg(WALLET)), 16)
    w["allowance_usdc_aave_to_pool"] = int(
        call(USDC_AAVE, sel("allowance(address,address)") + addr_arg(WALLET) + addr_arg(POOL)), 16
    )
    w["code_len"] = (len(rpc("eth_getCode", [WALLET, "latest"])) - 2) // 2
    out["wallet"] = w

    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
