#!/usr/bin/env python3
"""Preflight for the Base Sepolia operations, with real simulations.

READ-ONLY. Every call here is `eth_call`; nothing is signed and nothing is
broadcast. Supply uses balance/allowance overrides. Withdraw has no position
in this independent simulation and may revert. Calls do not update state for
subsequent calls. Exit 2 means incomplete sequence; exit 3 means wrong RPC chain.

A successful simulation is NOT proof of execution. It says the call would not
revert against the state it was simulated against. It says nothing about
whether KeeperHub will broadcast it, whether the sponsored path will engage, or
what the transaction will do at a later block.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import httpx
from eth_utils import function_signature_to_4byte_selector, keccak, to_checksum_address

RPC = "https://sepolia.base.org"
CHAIN_ID = 84532

POOL = "0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27"
USDC = "0xba50Cd2A20f6DA35D788639E581bca8d0B5d4D5f"
AUSDC = "0x10F1A9D11CDf50041f3f8cB7191CBE2f31750ACC"
FAUCET = "0xD9145b5F45Ad4519c7ACcD6E0A4A82e83bB8A6Dc"
WALLET = "0x7BEa5c59c12aebF9343087EF1513f651678291F5"

ONE_USDC = 1_000_000
MAX_UINT256 = 2**256 - 1

# OpenZeppelin ERC20 layout, confirmed on this token: _allowances is base slot
# 1 (a live holder's pool allowance hashes to it exactly), so _balances is 0.
BALANCES_SLOT = 0
ALLOWANCES_SLOT = 1

_id = [0]
_block_tag = "latest"


def rpc(method: str, params: list[Any]) -> dict[str, Any]:
    _id[0] += 1
    with httpx.Client(timeout=60, trust_env=False) as c:
        r = c.post(RPC, json={"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params})
        return r.json()


def sel(sig: str) -> str:
    return "0x" + function_signature_to_4byte_selector(sig).hex()


def enc_addr(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def enc_uint(n: int) -> str:
    return f"{n:064x}"


def map_slot(key: str, base: int) -> str:
    return "0x" + keccak(bytes.fromhex(enc_addr(key)) + base.to_bytes(32, "big")).hex()


def nested_slot(owner: str, spender: str, base: int) -> str:
    outer = map_slot(owner, base)
    return "0x" + keccak(bytes.fromhex(enc_addr(spender)) + bytes.fromhex(outer[2:])).hex()


def simulate(to: str, data: str, *, value: int = 0, overrides: dict | None = None) -> dict[str, Any]:
    call = {"from": WALLET, "to": to, "data": data}
    if value:
        call["value"] = hex(value)
    params: list[Any] = [call, _block_tag]
    if overrides:
        params.append(overrides)
    body = rpc("eth_call", params)
    if "result" in body:
        return {"ok": True, "returnData": body["result"]}
    err = body.get("error", {})
    return {"ok": False, "error": err.get("message"), "code": err.get("code"), "data": err.get("data")}


def op(name: str, target: str, sig: str, args: list[Any], encoded_args: str, *, value: int = 0,
       overrides: dict | None = None, note: str = "") -> dict[str, Any]:
    data = sel(sig) + encoded_args
    sim = simulate(target, data, value=value, overrides=overrides)
    return {
        "operation": name,
        "chainId": CHAIN_ID,
        "sender_from": WALLET,
        "target_to": to_checksum_address(target),
        "function": sig,
        "selector": sel(sig),
        "decoded_args": args,
        "calldata": data,
        "value_wei": value,
        "value_ether_string": "0" if value == 0 else str(value / 10**18),
        "simulation": sim,
        "simulated_with_state_overrides": bool(overrides),
        "state_overrides": overrides or None,
        "note": note,
    }


def main() -> int:
    global _block_tag
    reported_chain = int(rpc("eth_chainId", [])["result"], 16)
    if reported_chain != CHAIN_ID:
        print(json.dumps({"sequence_ready": False, "error": "wrong RPC chain",
                          "expected": CHAIN_ID, "observed": reported_chain}))
        return 3
    _block_tag = rpc("eth_blockNumber", [])["result"]
    out: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rpc": RPC,
        "chain_id_reported": reported_chain,
        "block_number": int(_block_tag, 16),
        "wallet": WALLET,
        "disclaimer": (
            "eth_call only. Nothing signed, nothing broadcast. A successful "
            "simulation is not proof of execution."
        ),
    }

    # Confirm the balance slot before relying on it: override it and read back.
    probe_override = {USDC: {"stateDiff": {map_slot(WALLET, BALANCES_SLOT): "0x" + enc_uint(12345)}}}
    probe = simulate(USDC, sel("balanceOf(address)") + enc_addr(WALLET), overrides=probe_override)
    out["state_override_supported"] = probe["ok"] and probe.get("returnData", "").endswith(f"{12345:x}")
    out["balances_slot_confirmed"] = out["state_override_supported"]
    out["balances_slot_probe"] = probe

    funded = {
        USDC: {
            "stateDiff": {
                map_slot(WALLET, BALANCES_SLOT): "0x" + enc_uint(ONE_USDC),
                nested_slot(WALLET, POOL, ALLOWANCES_SLOT): "0x" + enc_uint(MAX_UINT256),
            }
        }
    }

    ops = [
        op(
            "0. zero-value probe: set the pool's USDC allowance to 0",
            USDC,
            "approve(address,uint256)",
            [POOL, 0],
            enc_addr(POOL) + enc_uint(0),
            note=(
                "Chosen as the probe because it is a real state-changing write "
                "with value 0 that emits an Approval log we can bind on, and "
                "because the allowance is already 0 (evidence/01), so it is a "
                "zero allowance remains zero. This is not a read: sponsored "
                "execution may install persistent EIP-7702 delegation."
            ),
        ),
        op(
            "1. faucet mint 1 test USDC",
            FAUCET,
            "mint(address,address,uint256)",
            [USDC, WALLET, ONE_USDC],
            enc_addr(USDC) + enc_addr(WALLET) + enc_uint(ONE_USDC),
            note=(
                "Historical isPermissioned() read was false. evidence/05 suggested "
                "a 1e12 raw cap but did not retain pinned boundary RPC responses; "
                "that exact cap remains unverified. This simulates 1e6 raw. Emits "
                "Transfer(0 -> wallet, 1000000) on the token."
            ),
        ),
        op(
            "2. approve the pool for MAX_UINT256",
            USDC,
            "approve(address,uint256)",
            [POOL, MAX_UINT256],
            enc_addr(POOL) + enc_uint(MAX_UINT256),
            note=(
                "MAX_UINT256, not the supply amount: this is what the Wayfinder "
                "adapter emits -- lend() calls ensure_allowance(..., "
                "approval_amount=MAX_UINT256) (adapter.py:790-800)."
            ),
        ),
        op(
            "3. supply 1 USDC to Aave",
            POOL,
            "supply(address,uint256,address,uint16)",
            [USDC, ONE_USDC, WALLET, 0],
            enc_addr(USDC) + enc_uint(ONE_USDC) + enc_addr(WALLET) + enc_uint(0),
            overrides=funded,
            note=(
                "Simulated with the balance and allowance overridden, because "
                "the wallet holds 0 USDC right now. onBehalfOf is our own "
                "wallet; referralCode 0 is the adapter's REFERRAL_CODE."
            ),
        ),
        op(
            "4. withdraw 1 USDC from Aave",
            POOL,
            "withdraw(address,uint256,address)",
            [USDC, ONE_USDC, WALLET],
            enc_addr(USDC) + enc_uint(ONE_USDC) + enc_addr(WALLET),
            note=(
                "No supplied position in this independent eth_call. This script "
                "does not implement scaled aToken/index overrides. A separate "
                "stateful simulation or a later authorized execution must check "
                "withdraw after supply prerequisites."
            ),
        ),
    ]
    out["operations"] = ops

    out["sponsorship_expectation"] = {
        "expected_to_be_sponsored": "unverified",
        "why": [
            "Base Sepolia (84_532) is in SPONSORSHIP_CHAINS "
            "(lib/web3/sponsorship-chains-meta.ts:30) -- SOURCE, pinned self-host.",
            "Hosted /api/gas-sponsorship returns enabled:true, freeCents:100 -- OBSERVED.",
            "Hosted /api/chains reports 84532 isEnabled:true, usePrivateMempoolRpc:false, "
            "so the private-mempool gate (write-contract-core.ts:495) does not block it -- OBSERVED.",
            "Effective signer, org-specific gas credits, real mcp:write and sponsored submit "
            "remain unverified. months=[] and public freeCents do not establish org balance.",
        ],
        "if_not_sponsored": (
            "STOP and reconcile the original operation. Missing sponsorship evidence "
            "does not authorize funding, resubmission, another ID or another executor."
        ),
    }

    out["expected_onchain_shape_if_sponsored"] = {
        "top_level_from": "a Turnkey Gas Station relayer EOA, NOT our wallet",
        "top_level_to": "an executor contract, NOT the Aave pool",
        "top_level_value": 0,
        "our_call": "an internal call; only the receipt's event logs and a trace show it",
        "wallet_side_effects": [
            "A first authorization may install persistent EIP-7702 delegation code "
            "starting 0xef0100; actual hosted write shape remains unverified",
            "Authorization processing advances the authority nonce, but reuse of existing "
            "delegation need not. Unchanged nonce proves no absence for direct or sponsored sends",
        ],
        "source": (
            "tests/unit/verify-receipt.test.ts:558-562 ('Turnkey's Gas Station delegates "
            "the org wallet via EIP-7702, and a relayer EOA calls an executor contract "
            "which invokes the wallet'); docs/wallet-management/onchain-appearance.md:29-42"
        ),
        "consequence_for_msg_sender": (
            "Under EIP-7702 the delegated code executes in our EOA's own context, so "
            "msg.sender at the Aave pool is our wallet. That is why the approve from our "
            "wallet to the pool is the right allowance and why Supply.user must equal our "
            "wallet. INFERRED from the mechanism; not yet OBSERVED on this deployment."
        ),
    }

    out["all_independent_simulations_ok"] = all(x["simulation"]["ok"] for x in ops)
    out["stateful_sequence_verified"] = False
    out["sequence_ready"] = False
    out["status"] = "incomplete: independent eth_calls do not witness the sequential state"
    json.dump(out, sys.stdout, indent=2)
    print()
    return 2


if __name__ == "__main__":
    sys.exit(main())
