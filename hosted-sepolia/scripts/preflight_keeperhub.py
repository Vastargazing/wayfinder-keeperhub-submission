#!/usr/bin/env python3
"""Preflight, KeeperHub's side: dry-run each call through hosted KeeperHub.

`POST /api/execute/contract-call` with `"simulate": true` is a dry run. SOURCE,
pinned `stand/local-fork` @ 57b7be4:

  app/api/execute/contract-call/route.ts:303-307  "A dry run never signs,
      broadcasts, or reserves, so mcp:read satisfies it."
  app/api/execute/contract-call/route.ts:381-388  the simulate branch returns
      before the concurrency gate, before the idempotency reservation, and
      before handleWriteCall.
  lib/execute/simulate.ts                          uses estimateGas + provider.call only.

So this file signs nothing and broadcasts nothing. It is run before the human
confirmation on purpose: it is the only way to learn KeeperHub's *own*
interpretation of the request, including the stablecoin ceiling, before asking
for permission to send.

The API key is read from the environment and never written to the output.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

KEY = os.environ["KEEPERHUB_API_KEY"]
BASE = os.environ.get("KEEPERHUB_BASE_URL", "https://app.keeperhub.com").rstrip("/")
CHAIN = os.environ.get("KEEPERHUB_CHAIN_ID", "84532")
WALLET = os.environ["KEEPERHUB_WALLET_ADDRESS"]

POOL = "0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27"
USDC = "0xba50Cd2A20f6DA35D788639E581bca8d0B5d4D5f"
FAUCET = "0xD9145b5F45Ad4519c7ACcD6E0A4A82e83bB8A6Dc"
ONE_USDC = 1_000_000
MAX_UINT256 = 2**256 - 1

ERC20_APPROVE_ABI = [
    {
        "type": "function",
        "name": "approve",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    }
]
FAUCET_ABI = [
    {
        "type": "function",
        "name": "mint",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]
POOL_ABI = [
    {
        "type": "function",
        "name": "supply",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
            {"name": "referralCode", "type": "uint16"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "withdraw",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "to", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


def scrub(t: str) -> str:
    return t.replace(KEY, "<redacted>") if t else t


def dry_run(label: str, contract: str, abi: list, fn: str, args: list, note: str = "") -> dict[str, Any]:
    body = {
        "network": CHAIN,
        "contractAddress": contract,
        "abi": json.dumps(abi),
        "functionName": fn,
        "functionArgs": json.dumps([str(a) for a in args]),
        "value": "0",
        "simulate": True,
    }
    started = time.time()
    try:
        with httpx.Client(timeout=120, trust_env=False) as c:
            r = c.post(
                f"{BASE}/api/execute/contract-call",
                headers={
                    "Authorization": f"Bearer {KEY}",
                    "content-type": "application/json",
                    "accept": "application/json",
                },
                json=body,
            )
        try:
            resp: Any = r.json()
        except Exception:  # noqa: BLE001
            resp = scrub(r.text[:1500])
        status = r.status_code
    except Exception as exc:  # noqa: BLE001
        resp, status = scrub(f"{type(exc).__name__}: {exc}"), None

    sent = dict(body)
    sent["abi"] = f"<{len(abi)} function ABI: {[f['name'] for f in abi]}>"
    return {
        "label": label,
        "note": note,
        "request": sent,
        "sender_is": WALLET,
        "http_status": status,
        "ms": int((time.time() - started) * 1000),
        "response": resp,
    }


def main() -> int:
    if CHAIN != "84532":
        print(json.dumps({"sequence_ready": False, "error": "expected Base Sepolia 84532"}))
        return 3
    out: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": BASE,
        "chain": CHAIN,
        "wallet": WALLET,
        "method": "POST /api/execute/contract-call with simulate:true (mcp:read; never signs or broadcasts)",
        "runs": [],
    }
    runs = out["runs"]
    runs.append(dry_run("0. zero-value probe: approve(pool, 0)", USDC, ERC20_APPROVE_ABI, "approve", [POOL, 0],
                        "Real write, value 0, emits Approval. Zero allowance remains zero; a write may also install persistent delegation."))
    runs.append(dry_run("1. faucet mint 1 USDC", FAUCET, FAUCET_ABI, "mint", [USDC, WALLET, ONE_USDC],
                        "Historical isPermissioned() was false; exact cap is unverified (see evidence/CORRECTIONS.json)."))
    runs.append(dry_run("2a. approve(pool, MAX_UINT256)", USDC, ERC20_APPROVE_ABI, "approve", [POOL, MAX_UINT256],
                        "What the Wayfinder adapter actually emits (ensure_allowance approval_amount=MAX_UINT256). "
                        "Tests KeeperHub's stablecoin ceiling: docs/api/direct-execution.md:47 says an approve "
                        "above the 100 USD limit is allowed only when the spender belongs to a protocol integration."))
    runs.append(dry_run("2b. approve(pool, 1 USDC) [bounded alternative]", USDC, ERC20_APPROVE_ABI, "approve", [POOL, ONE_USDC],
                        "Separate alternative; after submit require proven pre-send refusal/quiescence or reconciled final outcome before changing authorization."))
    runs.append(dry_run("3. supply(USDC, 1e6, wallet, 0)", POOL, POOL_ABI, "supply", [USDC, ONE_USDC, WALLET, 0],
                        "Expected to fail today: the wallet holds 0 USDC and has no allowance. Re-run after 1 and 2."))
    runs.append(dry_run("4. withdraw(USDC, 1e6, wallet)", POOL, POOL_ABI, "withdraw", [USDC, ONE_USDC, WALLET],
                        "Expected to fail today: no aUSDC. Re-run after the supply lands."))

    out["all_independent_simulations_ok"] = all(
        r["http_status"] == 200 and isinstance(r["response"], dict)
        and r["response"].get("wouldRevert") is False for r in runs)
    out["stateful_sequence_verified"] = False
    out["sequence_ready"] = False
    json.dump(out, sys.stdout, indent=2, default=str)
    print()
    return 2


if __name__ == "__main__":
    sys.exit(main())
