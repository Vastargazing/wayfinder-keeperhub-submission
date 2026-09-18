"""Operator command for an indeterminate operation.

    inspect   read-only: what is blocked, and which hashes KeeperHub's own record
              binds to each blocked operation (plus diagnostic hints, labelled)
    verify    read-only: run the reconciliation gate and print the verdict
    resolve   run the gate and, ONLY if ownership AND content both pass, record

There is no `--force`, no `--yes`, no attestation path and no way to write without
the comparison. `resolve` calls `moonwell_demo.gate.resolve_operation`, whose write
step re-runs the entire verification at the moment of the write and takes the hash
it writes from that fresh run — so a verdict printed by `verify` earlier, or by a
different process, or against a different journal, cannot be applied.

Two independent checks decide, and both must pass:

* **ownership** — the hash must appear in KeeperHub's own record for THIS operation
  id (`workflow_executions.transaction_hashes`, the write-contract node's log
  output, or `pending_transactions` keyed by that execution). Identical operations
  produce identical envelopes by construction, so content can never establish
  ownership.
* **content** — the five journalled fields (chain id, sender, recipient, calldata,
  value) must match the envelope the SDK authorized before the send, because
  KeeperHub's recorded input has been OBSERVED to diverge from what executed.

`inspect` separates the two kinds of candidate it can offer. Hashes under
`operation_bound_candidates` come from KeeperHub's record for that operation and
are the only ones the gate can ever accept. Hashes under `diagnostic_hints` come
from a calldata scan of the chain: identical calldata identifies content, not an
operation, and the gate refuses them however well they match.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "integration"))
sys.path.insert(0, str(ROOT.parent / "wayfinder" / "upstream"))

from moonwell_demo.gate import (  # noqa: E402
    KeeperHubOwnershipOracle,
    KeeperHubSql,
    ReconciliationRefused,
    RpcChainReader,
    read_authorized_operation,
    resolve_operation,
    verify_authorized_operation,
)
from moonwell_demo.wiring import RPC  # noqa: E402

EV = ROOT / "evidence"
STATE = ROOT / "state"


def journal_path(state_dir: Path) -> Path:
    return state_dir / "sdk-journal.sqlite"


def workflow_id() -> str:
    return (EV / "00-workflow-id.txt").read_text().strip()


def build_oracle(client=None) -> KeeperHubOwnershipOracle:
    """The ownership side. Reads only; never writes and never guesses."""
    from moonwell_demo.wiring import CHAIN_ID, WALLET  # noqa: PLC0415

    return KeeperHubOwnershipOracle(
        workflow_id=workflow_id(),
        chain_id=CHAIN_ID,
        wallet_address=WALLET,
        sql=KeeperHubSql(),
        client=client,
    )


async def _lookup_evidence(state_dir: Path, operation_id: str) -> dict:
    """Ask the executor what it can say. Read-only."""
    from keeperhub_executor import KeeperHubClient  # noqa: PLC0415
    from keeperhub_executor.reconcile import KeeperHubDbProbe  # noqa: PLC0415

    from moonwell_demo.wiring import (  # noqa: PLC0415
        API_KEY_FILE,
        CHAIN_ID,
        KH_BASE,
        WALLET,
        DemoKeeperHubExecutor,
        demo_abi_resolver,
        register_demo_abis,
    )

    register_demo_abis()
    client = KeeperHubClient(KH_BASE, API_KEY_FILE.read_text().strip())
    ex = DemoKeeperHubExecutor(
        execution_profile="direct",
        client=client,
        workflow_id=workflow_id(),
        wallet_address=WALLET,
        chain_id=CHAIN_ID,
        rpc_url=RPC,
        abi_resolver=demo_abi_resolver,
        db_probe=KeeperHubDbProbe(),
    )
    from keeperhub_executor.authorization import ReadOnlyJournal
    journal = ReadOnlyJournal(state_dir / 'sdk-journal.sqlite')
    ex.bind_journal(journal)
    try:
        result, ev = await ex._lookup_with_evidence(operation_id)
        return {"outcome": result.outcome.value, "detail": result.detail, "evidence": ev.as_dict()}
    finally:
        journal.close()
        await ex.close()
        await client.close()


def _diagnostic_hints(lookup: dict) -> list[dict]:
    """Chain-scan candidates. NEVER ownership; shown so a human can see them."""
    ev = lookup.get("evidence") or {}
    out = []
    for m in ev.get("chainMatches") or []:
        out.append({
            "hash": m.get("hash"),
            "source": "diagnostic chain scan (same calldata; NOT an operation id)",
            "block": m.get("block"),
            "nonce": m.get("nonce"),
            "acceptable": False,
            "why": "the gate refuses this unless KeeperHub's record for this "
                   "operation also binds it",
        })
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["inspect", "verify", "resolve"])
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--operation-id")
    ap.add_argument("--tx-hash")
    ap.add_argument("--out")
    args = ap.parse_args()

    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        state_dir = STATE / state_dir
    jp = journal_path(state_dir)
    # Deliberately not an operator flag: an operator-chosen RPC is an
    # operator-chosen reality, and the content check reads the chain through it.
    reader = RpcChainReader(RPC)
    oracle = build_oracle()

    if args.action == "inspect":
        import sqlite3

        conn = sqlite3.connect(f"file:{jp}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT operation_id, state, consumed, txn_hash, envelope FROM operations ORDER BY created_at"
        ).fetchall()
        conn.close()
        report = {
            "journal": str(jp),
            "workflowId": workflow_id(),
            "rows": [],
            "blocked": [],
            "note": "only `operation_bound_candidates` can ever be accepted; "
                    "`diagnostic_hints` are calldata matches and are refused",
        }
        for op, state, consumed, txh, env in rows:
            envelope = json.loads(env)
            item = {
                "operation_id": op,
                "state": state,
                "consumed": bool(consumed),
                "txn_hash": txh,
                "selector": (envelope.get("data") or "0x")[:10],
                "to": envelope.get("to"),
                "value": envelope.get("value"),
            }
            report["rows"].append(item)
            if state == "pending":
                lookup = await _lookup_evidence(state_dir, op)
                finding = await oracle.ownership(op, "0x" + "00" * 32)
                report["blocked"].append(
                    {
                        **item,
                        "authorized_envelope": envelope,
                        "executor_lookup": {"outcome": lookup["outcome"], "detail": lookup["detail"]},
                        "operation_bound_candidates": [
                            {**o.as_dict(), "acceptable": True,
                             "why": "KeeperHub's own record binds this hash to this operation id; "
                                    "the five content checks still have to pass"}
                            for o in finding.owned
                        ],
                        "diagnostic_hints": _diagnostic_hints(lookup),
                        "ownership": finding.as_dict(),
                        "lookup_evidence": lookup["evidence"],
                    }
                )
        text = json.dumps(report, indent=1, default=str)
        (Path(args.out) if args.out else EV / f"operator-inspect-{state_dir.name}.json").write_text(text)
        for b in report["blocked"]:
            print(f"BLOCKED {b['operation_id']} selector={b['selector']} -> {b['executor_lookup']['outcome']}")
            for c in b["operation_bound_candidates"]:
                print(f"   operation-bound {c['hash']}  ({c['source']})")
            for c in b["diagnostic_hints"]:
                print(f"   hint (NOT acceptable) {c['hash']}  ({c['source']})")
            if not b["operation_bound_candidates"]:
                print("   no hash is bound to this operation by KeeperHub's record: "
                      "it cannot be resolved, and it must stay blocked")
        if not report["blocked"]:
            print("nothing pending")
        return

    if not args.operation_id or not args.tx_hash:
        ap.error("--operation-id and --tx-hash are required for verify/resolve")

    authorized = read_authorized_operation(jp, args.operation_id)
    if args.action == "verify":
        verdict = await verify_authorized_operation(authorized, args.tx_hash, reader, oracle)
        payload = {"action": "verify", "journal": str(jp), "verdict": verdict.as_dict(),
                   "authorized_envelope": authorized.envelope, "wrote_anything": False,
                   "note": "a passing verify is not a resolution: `resolve` re-runs the "
                           "whole check at the moment it writes"}
        text = json.dumps(payload, indent=1, default=str)
        (Path(args.out) if args.out else EV / f"operator-verify-{args.operation_id[:8]}.json").write_text(text)
        print(text)
        sys.exit(0 if verdict.ok else 2)

    # resolve
    try:
        verdict, written = await resolve_operation(jp, args.operation_id, args.tx_hash, reader, oracle)
        payload = {"action": "resolve", "journal": str(jp), "verdict": verdict.as_dict(),
                   "authorized_envelope": authorized.envelope, "written": written, "wrote_anything": True}
        code = 0
    except ReconciliationRefused as exc:
        payload = {
            "action": "resolve",
            "journal": str(jp),
            "refused": str(exc),
            "verdict": exc.verdict.as_dict() if exc.verdict else None,
            "authorized_envelope": authorized.envelope,
            "wrote_anything": False,
        }
        code = 2
    text = json.dumps(payload, indent=1, default=str)
    (Path(args.out) if args.out else EV / f"operator-resolve-{args.operation_id[:8]}.json").write_text(text)
    print(text)
    sys.exit(code)


asyncio.run(main())
