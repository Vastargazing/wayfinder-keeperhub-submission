"""Build state directories the way a run writes them.

These are test fixtures. They use the real ``OperationJournal`` so their shape
cannot drift from what the SDK produces, and nothing in this suite presents one
as a run that happened.
"""

import hashlib
import json
from pathlib import Path

WALLET = "0x0000000000000000000000000000000000f1c700"
TOKEN = "0x0000000000000000000000000000000000000010"
POOL = "0x0000000000000000000000000000000000000020"
ROUTER = "0x0000000000000000000000000000000000000030"
CHAIN_ID = 8453
RUN_ID = "a" * 32
ROOT = f"{RUN_ID}/iteration/2"

BORROW = f"{ROOT}/borrow/0"
WRAP = f"{ROOT}/wrap_eth/0"
APPROVE = f"{ROOT}/ensure-allowance/approve"
LEND = f"{ROOT}/lend/0"

#: (call key, to, calldata, checkpoint state, journal outcome)
DEFAULT_STEPS = (
    (BORROW, POOL, "0xc5ebeaec" + "1" * 64, "done", "landed"),
    (WRAP, POOL, "0xd0e30db0", "done", "landed"),
    (APPROVE, TOKEN, "0x095ea7b3" + "2" * 64, "started", "pending"),
    (LEND, POOL, "0xa0712d68" + "4" * 64, "started", "unbound"),
)

FORK_MODE_LABELS = {
    "chain": "FORK (anvil fork of Base mainnet, chain id 8453)",
    "signer": "DEV/TEST SIGNER (an in-process ethers.Wallet patch, not Turnkey)",
    "keeperhub": "SELF-HOSTED (local docker build of the public repo)",
    "sdk_key_material": "NONE (no private key, no Wayfinder API key in the SDK process)",
    "swap_quote": "STAND-IN (LI.FI keyless quote, not BRAP)",
    "token_metadata_and_prices": "STUB",
}


def write_run(directory, *, steps=DEFAULT_STEPS, run_id=RUN_ID, mode_labels=None,
              corroboration=None, manifest_state_dir=None, marker=None,
              quote=None, write_manifest=True):
    """Write a state directory the way a run writes one, and return its path."""
    from wayfinder_paths.core.utils.executor import OperationJournal, build_envelope

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if marker is not None:
        (directory / "FIXTURE.txt").write_text(marker)

    plan = {
        "version": 2,
        "run_id": run_id,
        "strategy": "moonwell-wsteth-loop",
        "seed": {"usdc_amount": 0.0, "state": "external"},
        "iterations": [
            {"index": 1, "borrow_amt_wei": 10**17, "borrowable_wei_before": 10**20,
             "status": "done", "lend_amt_wei": 5 * 10**16, "started_at": 0.0},
            {"index": 2, "borrow_amt_wei": 8 * 10**16, "borrowable_wei_before": 9 * 10**19,
             "status": "in_flight", "started_at": 0.0},
        ],
        "calls": {},
    }

    journal = OperationJournal(directory / "sdk-journal.sqlite")
    try:
        for index, (key, to, data, call_state, outcome) in enumerate(steps):
            call = {"intent": {"args": [], "kwargs": {}}, "state": call_state,
                    "money": True, "composite": False}
            if outcome != "unbound":
                envelope = build_envelope(
                    {"chainId": CHAIN_ID, "from": WALLET, "to": to, "data": data, "value": 0}
                )
                row, _ = journal.bind_step(f"{key}/send/0", envelope)
                if outcome == "landed":
                    txn = "0x" + f"{index + 1:064x}"
                    journal.record_hash(row["operation_id"], txn, consumed=True)
                    call["result"] = [True, txn]
                elif outcome == "never_seen":
                    journal.mark_never_seen(row["operation_id"])
            plan["calls"][key] = call
        if quote is not None:
            plan["calls"][f"{ROOT}/_swap_with_retries/0/best_quote"] = {
                "intent": {"args": [], "kwargs": {}}, "state": "done",
                "money": False, "composite": False, "result": [True, {}],
                **quote,
            }
        conn = journal._conn
        conn.execute(
            "CREATE TABLE IF NOT EXISTS moonwell_run"
            " (singleton INTEGER PRIMARY KEY CHECK(singleton=1), run_id TEXT NOT NULL)"
        )
        conn.execute("INSERT OR REPLACE INTO moonwell_run VALUES (1,?)", (run_id,))
    finally:
        journal.close()

    digest = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (directory / "run-plan.json").write_text(
        json.dumps({"data": plan, "sha256": digest}, sort_keys=True, indent=2)
    )

    if write_manifest:
        manifest = {
            "stateDir": str(manifest_state_dir or directory.resolve()),
            "run_id": run_id,
            "scenario": "test fixture",
            "mode_labels": dict(FORK_MODE_LABELS if mode_labels is None else mode_labels),
            "not_proven": ["Turnkey custody", "hosted KeeperHub", "mainnet or testnet execution"],
            "wallet": WALLET,
            "chainId": CHAIN_ID,
            "corroboration": corroboration or {},
            "position": {"block": "0x1", "nonce": 4, "wallet": {"eth_wei": 1}},
        }
        (directory / "run-manifest.json").write_text(json.dumps(manifest, indent=1))
    return directory
