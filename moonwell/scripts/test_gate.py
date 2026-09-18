"""Tests for the operator reconciliation gate (revision 2: ownership + content).

Run: `python scripts/test_gate.py [--out evidence/61-gate-tests.json]`

Every case builds a real `OperationJournal` (the seam's own SQLite class, so the
authorization row is written by `journal.begin` exactly as a live run writes it),
then asks the gate about a candidate transaction. Most cases use a fake chain
reader and a fake ownership oracle so that "wrong chain id", "KeeperHub cannot be
read" and friends can be produced exactly. The `--real-*` group uses the **live
fork and the live KeeperHub database**, so the headline cases are not synthetic.

The three defects the review found are each covered by their own group:

* `own_*`   — content is not ownership. A transaction that matches all five
              fields is refused unless KeeperHub's record for THIS operation id
              binds it. Includes the case where the hash is in no record at all,
              which the consumed-hash ban cannot catch.
* `mut_*`   — a verdict is bound to what it was computed from. `replace`,
              `object.__setattr__`, a hand-built verdict, a copied seal and a
              subclass that lies about `ok` are all refused.
* `apply_*` — applying a verdict re-runs everything. A verdict from another
              journal, from before the envelope changed, from before ownership
              was revoked, or from before another row claimed the hash, is
              refused at the write.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import re
import sqlite3
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "integration"))
sys.path.insert(0, str(ROOT.parent / "wayfinder" / "upstream"))

from wayfinder_paths.core.utils.executor import OperationJournal  # noqa: E402

from moonwell_demo.gate import (  # noqa: E402
    FieldCheck,
    KeeperHubOwnershipOracle,
    KeeperHubSql,
    OwnedHash,
    OwnershipFinding,
    ReconciliationRefused,
    ReconciliationVerdict,
    RpcChainReader,
    envelope_digest,
    journal_key,
    mark_resolved,
    read_authorized_operation,
    resolve_operation,
    verify_authorized_operation,
)

CHAIN = 8453
SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
OTHER_SENDER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
M_WETH = "0x628ff693426583D9a7FB391E54366292F509D457"
M_WSTETH = "0x627Fe393Bc6EdDA28e99AE648fD6fF362514304b"
# borrow(uint256) with 0.5 WETH
AUTH_DATA = "0xc5ebeaec" + format(5 * 10**17, "064x")
ALTERED_AMOUNT_DATA = "0xc5ebeaec" + format(5 * 10**18, "064x")  # 10x the borrow
HASH = "0x" + "ab" * 32
OTHER_HASH = "0x" + "cd" * 32

RESULTS: list[dict[str, Any]] = []


def record(name: str, passed: bool, **extra: Any) -> None:
    RESULTS.append({"case": name, "passed": bool(passed), **extra})
    print(("PASS " if passed else "FAIL ") + name
          + ("" if passed else "  <<< " + json.dumps(extra, default=str)[:400]))


def authorized_envelope(**over: Any) -> dict[str, Any]:
    env = {"chainId": CHAIN, "from": SENDER, "to": M_WETH, "data": AUTH_DATA, "value": 0}
    env.update(over)
    return env


class FakeChainReader:
    """A chain that says whatever a case needs it to say."""

    def __init__(self, tx: dict[str, Any] | None, *, chain_id: int = CHAIN,
                 receipt: dict[str, Any] | None = None, raise_on_tx: str | None = None) -> None:
        self._tx = tx
        self._chain_id = chain_id
        self._receipt = receipt if receipt is not None else {
            "status": "0x1", "blockNumber": "0x3", "gasUsed": "0x5208"}
        self._raise = raise_on_tx

    async def chain_id(self) -> int:
        return self._chain_id

    async def transaction(self, txn_hash: str):
        if self._raise:
            raise RuntimeError(self._raise)
        return self._tx

    async def receipt(self, txn_hash: str):
        return self._receipt


class FakeOracle:
    """KeeperHub's record, stubbed. `owns` never consults `hints`, by construction."""

    def __init__(self, *, owned: tuple[str, ...] = (), available: bool = True,
                 ambiguous: bool = False, foreign: tuple[dict, ...] = (),
                 hints: tuple[str, ...] = (), raises: str | None = None,
                 execution_id: str | None = "kh-execution-1",
                 foreign_scan_complete: bool = True) -> None:
        self.owned = owned
        self.available = available
        self.ambiguous = ambiguous
        self.foreign = foreign
        self.hints = hints
        self.raises = raises
        self.execution_id = execution_id
        self.foreign_scan_complete = foreign_scan_complete
        self.calls = 0

    async def ownership(self, operation_id: str, candidate_hash: str) -> OwnershipFinding:
        self.calls += 1
        if self.raises:
            raise RuntimeError(self.raises)
        return OwnershipFinding(
            operation_id,
            available=self.available,
            ambiguous=self.ambiguous,
            owned=tuple(OwnedHash(h.lower(), "fake:workflow_executions.transaction_hashes")
                        for h in self.owned),
            foreign_bindings=tuple(self.foreign),
            hints=tuple({"hash": h, "source": "fake:chain scan"} for h in self.hints),
            execution_id=self.execution_id,
            foreign_scan_complete=self.foreign_scan_complete,
            detail="fake oracle",
        )


def tx(**over: Any) -> dict[str, Any]:
    base = {
        "hash": HASH,
        "chainId": hex(CHAIN),
        "from": SENDER,
        "to": M_WETH,
        "input": AUTH_DATA,
        "value": "0x0",
        "nonce": "0x1",
        "blockNumber": "0x3",
        "type": "0x2",
    }
    base.update(over)
    return base


def new_journal(tmp: Path, envelope: dict[str, Any], *, name: str = "sdk-journal.sqlite") -> tuple[Path, str]:
    path = tmp / name
    j = OperationJournal(path)
    op = j.begin(envelope)  # the pre-send authorization, exactly as a live run writes it
    j.close()
    return path.resolve(), op


def insert_row(path: Path, operation_id: str, envelope: dict[str, Any], *,
               state: str = "pending", txn_hash: str | None = None) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "INSERT INTO operations (operation_id, sender, chain_id, digest, envelope, state, txn_hash)"
        " VALUES (?,?,?,?,?,?,?)",
        (operation_id, envelope["from"].lower(), int(envelope["chainId"]),
         envelope_digest(envelope), json.dumps(envelope, sort_keys=True), state, txn_hash),
    )
    conn.close()


def set_envelope(path: Path, operation_id: str, envelope: dict[str, Any]) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "UPDATE operations SET envelope=?, digest=? WHERE operation_id=?",
        (json.dumps(envelope, sort_keys=True), envelope_digest(envelope), operation_id),
    )
    conn.close()


def row(path: Path, op: str) -> tuple:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT state, txn_hash, consumed FROM operations WHERE operation_id=?", (op,)
        ).fetchone()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# the generic case runner
# --------------------------------------------------------------------------


async def case(name: str, *, expect_ok: bool, reader, oracle, envelope=None, candidate=HASH,
               expect_fields: list[str] | None = None, expect_blocker: str | None = None,
               mutate=None, note: str = "") -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path, op = new_journal(tmp, envelope or authorized_envelope())
        if mutate:
            mutate(path, op)
        authorized = read_authorized_operation(path, op)
        verdict = await verify_authorized_operation(authorized, candidate, reader, oracle)
        before = row(path, op)
        wrote = None
        error = None
        try:
            wrote = await mark_resolved(path, verdict, reader, oracle)
        except ReconciliationRefused as exc:
            error = str(exc)
        after = row(path, op)

        ok = verdict.ok is expect_ok
        if expect_fields is not None:
            ok = ok and sorted(verdict.failed_fields) == sorted(expect_fields)
        if expect_blocker is not None:
            ok = ok and any(expect_blocker in b for b in verdict.blockers)
        if expect_ok:
            ok = ok and wrote is not None and after[0] == "submitted" \
                 and after[1] == candidate.lower() and after[2] == 0
        else:
            # the crucial invariant: a refusal leaves the journal untouched
            ok = ok and wrote is None and error is not None and after == before

        record(
            name, ok,
            note=note,
            expected_ok=expect_ok,
            verdict_ok=verdict.ok,
            ownership_established=verdict.ownership_established,
            ownership_sources=list(verdict.ownership_sources),
            failed_fields=verdict.failed_fields,
            blockers=list(verdict.blockers),
            reason=verdict.reason(),
            journal_before={"state": before[0], "txn_hash": before[1], "consumed": before[2]},
            journal_after={"state": after[0], "txn_hash": after[1], "consumed": after[2]},
            wrote=wrote,
            refusal=error,
            checks=[c.as_dict() for c in verdict.checks],
        )


async def refuses_at_apply(name: str, *, build, note: str = "") -> None:
    """`build(path, op, reader, oracle) -> (verdict, apply_reader, apply_oracle)`.

    Asserts `mark_resolved` refuses and the journal row is byte-identical after.
    """
    with tempfile.TemporaryDirectory() as td:
        path, op, verdict, apply_path, apply_reader, apply_oracle, extra = await build(Path(td))
        before = row(apply_path, verdict.operation_id) if verdict.operation_id else None
        wrote = None
        error = None
        try:
            wrote = await mark_resolved(apply_path, verdict, apply_reader, apply_oracle)
        except ReconciliationRefused as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        after = row(apply_path, verdict.operation_id) if verdict.operation_id else None
        ok = wrote is None and error is not None and after == before
        extra.setdefault("note", note)
        record(name, ok, refusal=error, wrote=wrote,
               journal_before=before, journal_after=after, **extra)


# --------------------------------------------------------------------------
# groups
# --------------------------------------------------------------------------


async def content_cases() -> None:
    """The five required negatives keep passing. Ownership is granted so the
    content failure is the only thing under test."""
    owned = FakeOracle(owned=(HASH,))

    await case("control_owned_and_all_five_match", expect_ok=True,
               reader=FakeChainReader(tx()), oracle=owned,
               note="ownership established AND every field matches; the row becomes "
                    "submitted/unconsumed, which is what resolve_pending produces for a "
                    "landed lookup")

    await case("wrong_chain_id_rpc", expect_ok=False, oracle=owned,
               reader=FakeChainReader(tx(), chain_id=1), expect_fields=["chain_id"],
               note="RPC is on Ethereum, the operation authorized Base")
    await case("wrong_chain_id_tx_field", expect_ok=False, oracle=owned,
               reader=FakeChainReader(tx(chainId=hex(10))), expect_fields=["chain_id"],
               note="the transaction's own chainId is Optimism")
    await case("wrong_sender", expect_ok=False, oracle=owned,
               reader=FakeChainReader(tx(**{"from": OTHER_SENDER})),
               expect_fields=["sender"], note="a matching borrow made by a different wallet")
    await case("wrong_recipient", expect_ok=False, oracle=owned,
               reader=FakeChainReader(tx(to=M_WSTETH)), expect_fields=["recipient"],
               note="the same borrow calldata sent to a different market")
    await case("altered_calldata", expect_ok=False, oracle=owned,
               reader=FakeChainReader(tx(input=AUTH_DATA[:-1] + ("0" if AUTH_DATA[-1] != "0" else "1"))),
               expect_fields=["calldata"], note="one nibble different")
    await case("altered_amount", expect_ok=False, oracle=owned,
               reader=FakeChainReader(tx(input=ALTERED_AMOUNT_DATA)), expect_fields=["calldata"],
               note="10x the authorized borrow; the amount lives in the calldata")
    await case("altered_value", expect_ok=False, oracle=owned,
               reader=FakeChainReader(tx(value=hex(10**18))), expect_fields=["value"],
               note="1 ETH attached to a call authorized with value 0")

    await case("receipt_success_but_wrong_everything", expect_ok=False, oracle=owned,
               reader=FakeChainReader(tx(**{"from": OTHER_SENDER, "to": M_WSTETH,
                                            "input": ALTERED_AMOUNT_DATA, "value": hex(10**18)}),
                                      chain_id=1),
               expect_fields=["chain_id", "sender", "recipient", "calldata", "value"],
               note="exists, mined, receipt status 1, and KeeperHub even owns it -- and it is "
                    "still refused, because ownership does not excuse content")

    # preconditions
    await case("transaction_unknown_to_node", expect_ok=False, oracle=owned,
               reader=FakeChainReader(None), expect_blocker="does not know transaction")
    await case("transaction_not_mined", expect_ok=False, oracle=owned,
               reader=FakeChainReader(tx(blockNumber=None)), expect_blocker="not mined")
    await case("rpc_unreachable", expect_ok=False, oracle=owned,
               reader=FakeChainReader(tx(), raise_on_tx="connection refused"),
               expect_blocker="eth_getTransactionByHash failed")
    await case("malformed_candidate_hash", expect_ok=False, oracle=owned,
               reader=FakeChainReader(tx()), candidate="0xdeadbeef",
               expect_blocker="is not a 32-byte")

    def already_resolved(path: Path, op: str) -> None:
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("UPDATE operations SET state='submitted', txn_hash=?, consumed=1"
                     " WHERE operation_id=?", (OTHER_HASH, op))
        conn.close()

    await case("row_already_resolved", expect_ok=False, oracle=owned, reader=FakeChainReader(tx()),
               mutate=already_resolved, expect_blocker="not 'pending'",
               note="cannot overwrite a settled row")

    def tamper_envelope(path: Path, op: str) -> None:
        # change the envelope but leave the digest column alone
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("UPDATE operations SET envelope=? WHERE operation_id=?",
                     (json.dumps(authorized_envelope(data=ALTERED_AMOUNT_DATA), sort_keys=True), op))
        conn.close()

    await case("journal_row_digest_tampered", expect_ok=False, oracle=owned,
               reader=FakeChainReader(tx()), mutate=tamper_envelope,
               expect_blocker="does not match the digest of its own envelope",
               note="the authorization row was edited after it was written")

    # operation absent from the journal entirely
    with tempfile.TemporaryDirectory() as td:
        path, _ = new_journal(Path(td), authorized_envelope())
        try:
            read_authorized_operation(path, "no-such-operation")
            passed, detail = False, "no refusal"
        except ReconciliationRefused as exc:
            passed, detail = True, str(exc)
        record("operation_not_in_journal", passed, refusal=detail)

    # resolve_operation (the single entry point) must refuse too
    with tempfile.TemporaryDirectory() as td:
        path, op = new_journal(Path(td), authorized_envelope())
        try:
            await resolve_operation(path, op, HASH,
                                    FakeChainReader(tx(**{"from": OTHER_SENDER})), owned)
            passed, detail = False, "resolve_operation wrote on a mismatch"
        except ReconciliationRefused as exc:
            passed, detail = row(path, op)[0] == "pending", str(exc)
        record("resolve_operation_entry_point_refuses", passed, refusal=detail)


async def ownership_cases() -> None:
    """Defect 1. Content match without ownership is refused, in every shape."""
    all_five_match = FakeChainReader(tx())

    # ---- THE REVIEW'S CASE ------------------------------------------------
    # B has an identical envelope to A and never broadcast. KeeperHub has no
    # execution bound to B, so nothing is owned. A's hash matches B's five fields
    # perfectly. Revision 1 accepted it.
    await case("own_identical_envelope_other_operations_hash", expect_ok=False,
               reader=all_five_match,
               oracle=FakeOracle(owned=(), foreign=({"where": "workflow_executions.transaction_hashes",
                                                     "operation_id": "operation-A"},)),
               expect_fields=["ownership", "hash_not_consumed"],
               note="THE REVIEW'S CASE. Five-field content match, refused: KeeperHub binds "
                    "this hash to operation A, not to this one")

    # ---- THE REVIEW'S ADDITION -------------------------------------------
    # The same transaction, but bound to nothing anywhere. The consumed-hash ban
    # cannot see it. Only positive ownership refuses it.
    await case("own_old_tx_bound_to_nothing_anywhere", expect_ok=False,
               reader=all_five_match,
               oracle=FakeOracle(owned=(), foreign=()),
               expect_fields=["ownership"],
               note="REQUIRED BY THE REVIEW. All five fields match, the hash is in no journal "
                    "and in no KeeperHub record, so hash_not_consumed PASSES -- and it is still "
                    "refused, on ownership alone. Banning consumed hashes is insufficient")

    await case("own_chain_scan_hint_is_not_ownership", expect_ok=False,
               reader=all_five_match, oracle=FakeOracle(owned=(), hints=(HASH,)),
               expect_fields=["ownership"],
               note="the calldata scan offers exactly this hash; OwnershipFinding.owns never "
                    "looks at hints")

    await case("own_keeperhub_unreadable_is_unknown_not_absent", expect_ok=False,
               reader=all_five_match, oracle=FakeOracle(available=False),
               expect_blocker="ownership is UNKNOWN, not absent",
               note="the probe did not run; the operation stays blocked rather than being "
                    "closed on a guess")

    await case("own_ambiguous_two_executions", expect_ok=False,
               reader=all_five_match, oracle=FakeOracle(owned=(HASH,), ambiguous=True),
               expect_blocker="ownership is ambiguous",
               note="two executions carry the operation id; no hash can be attributed")

    await case("own_oracle_raises", expect_ok=False, reader=all_five_match,
               oracle=FakeOracle(raises="psql: connection refused"),
               expect_blocker="ownership lookup failed",
               note="an exception is not an answer")

    await case("own_no_oracle_at_all", expect_ok=False, reader=all_five_match, oracle=None,
               expect_blocker="no ownership oracle was supplied",
               note="a caller that forgets the oracle gets a refusal, never a pass")

    await case("own_owned_but_content_mismatch", expect_ok=False,
               reader=FakeChainReader(tx(to=M_WSTETH)), oracle=FakeOracle(owned=(HASH,)),
               expect_fields=["recipient"],
               note="KeeperHub says it broadcast this hash for this operation, but the "
                    "transaction is a different call -- KeeperHub's recorded input has been "
                    "OBSERVED to diverge, so content is still required")

    await case("own_hash_already_used_by_another_row_in_this_journal", expect_ok=False,
               reader=all_five_match, oracle=FakeOracle(owned=(HASH,)),
               expect_fields=["hash_not_consumed"],
               mutate=lambda p, op: insert_row(p, "older-operation", authorized_envelope(),
                                               state="submitted", txn_hash=HASH),
               note="even an owned hash is refused if another row in this journal already "
                    "claims it")

    await case("own_foreign_binding_scan_incomplete_blocks", expect_ok=False,
               reader=all_five_match,
               oracle=FakeOracle(owned=(HASH,), foreign_scan_complete=False),
               expect_blocker="UNKNOWN whether another operation already claims it",
               expect_fields=["hash_not_consumed"],
               note="ownership holds and all five fields match, but the query that would "
                    "have revealed a foreign binding could not run; 'not scanned' is not "
                    "'nothing found', so it blocks")

    await case("own_case_insensitive_hash_is_still_owned", expect_ok=True,
               reader=FakeChainReader(tx()), oracle=FakeOracle(owned=(HASH.upper(),)),
               note="ownership comparison is case-insensitive on both sides, and the value "
                    "written is canonical lower case")


async def mutation_cases() -> None:
    """Defect 2. A verdict is inseparable from what it was computed from."""
    reader = FakeChainReader(tx())
    oracle = FakeOracle(owned=(HASH, OTHER_HASH))

    with tempfile.TemporaryDirectory() as td:
        path, op = new_journal(Path(td), authorized_envelope())
        auth = read_authorized_operation(path, op)
        good = await verify_authorized_operation(auth, HASH, reader, oracle)
        record("mut_baseline_verdict_is_ok", good.ok and good.seal_valid,
               reason=good.reason(), seal=good.seal[:16])

        # A verdict that legitimately refuses, so that "clear the blockers" and
        # "flip ownership on" are real mutations rather than no-ops.
        blocked_reader = FakeChainReader(tx(blockNumber=None))
        refused = await verify_authorized_operation(
            auth, HASH, blocked_reader, FakeOracle(owned=(), available=False))
        record("mut_baseline_refused_verdict_is_not_ok",
               (not refused.ok) and refused.seal_valid and bool(refused.blockers),
               reason=refused.reason())

        mutations: dict[str, ReconciliationVerdict] = {
            "replace_candidate_hash": replace(good, candidate_hash=OTHER_HASH),
            "replace_operation_id": replace(good, operation_id="another-operation"),
            "replace_envelope_digest": replace(good, envelope_digest="00" * 32),
            "replace_journal_key": replace(good, journal_key="11" * 32),
            "replace_ownership_sources": replace(good, ownership_sources=("made up",)),
            "replace_checks_all_true": replace(good, checks=tuple(
                replace(c, match=True, observed=c.authorized) for c in good.checks)),
            "replace_blockers_cleared": replace(refused, blockers=()),
            "replace_ownership_available": replace(
                refused, ownership_available=True, ownership_sources=("made up",)),
            "replace_refused_checks_all_true": replace(
                refused, blockers=(), ownership_available=True,
                ownership_sources=("made up",),
                checks=tuple(replace(c, match=True, observed=c.authorized)
                             for c in refused.checks)),
        }
        for label, bad in mutations.items():
            raised = None
            try:
                bad.authorized_hash()
            except ReconciliationRefused as exc:
                raised = str(exc)
            record(f"mut_{label}", raised is not None and not bad.ok and not bad.seal_valid,
                   refusal=raised, seal_valid=bad.seal_valid, ok=bad.ok)

        # object.__setattr__ past the frozen dataclass
        sneaky = await verify_authorized_operation(auth, HASH, reader, oracle)
        object.__setattr__(sneaky, "candidate_hash", OTHER_HASH)
        record("mut_object_setattr_candidate_hash", not sneaky.seal_valid and not sneaky.ok,
               seal_valid=sneaky.seal_valid, candidate=sneaky.candidate_hash)

        # a hand-built verdict whose every check claims to match
        forged = ReconciliationVerdict(
            operation_id=op, candidate_hash=OTHER_HASH,
            envelope_digest=auth.computed_digest, journal_key=auth.journal_key,
            checks=tuple(FieldCheck(f, "x", "x", True) for f in
                         ("ownership", "hash_not_consumed", "chain_id", "sender",
                          "recipient", "calldata", "value")),
            ownership_sources=("made up",), ownership_available=True,
        )
        raised = None
        try:
            forged.authorized_hash()
        except ReconciliationRefused as exc:
            raised = str(exc)
        record("mut_hand_built_verdict_all_true", raised is not None and not forged.ok,
               refusal=raised)

        # the same forgery, but with a genuine verdict's seal copied onto it
        copied = replace(forged, seal=good.seal)
        raised = None
        try:
            copied.authorized_hash()
        except ReconciliationRefused as exc:
            raised = str(exc)
        record("mut_copied_seal_from_a_real_verdict", raised is not None and not copied.ok,
               refusal=raised,
               note="the seal covers the payload, so it does not transfer to another one")

        # and none of them can write
        for label, bad in [*mutations.items(), ("hand_built", forged), ("copied_seal", copied)]:
            before = row(path, op)
            err = None
            wrote = None
            try:
                wrote = await mark_resolved(path, bad, reader, oracle)
            except ReconciliationRefused as exc:
                err = str(exc)
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
            record(f"mut_mark_resolved_refuses_{label}",
                   wrote is None and err is not None and row(path, op) == before,
                   refusal=err)

    # a subclass that simply lies about `ok` -- apply-time re-verification, not the
    # seal, is what stops this one.
    class ForgedVerdict(ReconciliationVerdict):
        @property
        def seal_valid(self) -> bool:  # type: ignore[override]
            return True

        @property
        def ok(self) -> bool:  # type: ignore[override]
            return True

        def authorized_hash(self) -> str:  # type: ignore[override]
            return self.candidate_hash

    for label, cand, orc, why in [
        ("unowned_hash", HASH, FakeOracle(owned=()),
         "a subclass that overrides ok/seal_valid/authorized_hash still cannot write: "
         "mark_resolved re-runs the real comparison and ITS verdict refuses"),
        ("owned_but_wrong_content", HASH, FakeOracle(owned=(HASH,)), "same, on content"),
    ]:
        with tempfile.TemporaryDirectory() as td:
            rdr = FakeChainReader(tx() if label == "unowned_hash" else tx(to=M_WSTETH))
            path, op = new_journal(Path(td), authorized_envelope())
            auth = read_authorized_operation(path, op)
            evil = ForgedVerdict(
                operation_id=op, candidate_hash=cand,
                envelope_digest=auth.computed_digest, journal_key=auth.journal_key,
                checks=(FieldCheck("everything", "x", "x", True),),
                ownership_sources=("forged",), ownership_available=True,
            )
            before = row(path, op)
            err = None
            wrote = None
            try:
                wrote = await mark_resolved(path, evil, rdr, orc)
            except ReconciliationRefused as exc:
                err = str(exc)
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
            record(f"mut_subclass_lying_about_ok_{label}",
                   wrote is None and err is not None and row(path, op) == before,
                   refusal=err, note=why)


async def apply_time_cases() -> None:
    """Defect 3. The write re-reads the journal, re-derives, and re-runs."""
    reader = FakeChainReader(tx())

    # --- cross-journal: same operation id, different envelope --------------
    async def build_cross_journal(td: Path):
        oracle = FakeOracle(owned=(HASH,))
        j1, op = new_journal(td, authorized_envelope(), name="j1.sqlite")
        j2, _throwaway = new_journal(td, authorized_envelope(data=ALTERED_AMOUNT_DATA),
                                     name="j2.sqlite")
        # the same operation id denotes a different envelope in j2
        insert_row(j2, op, authorized_envelope(data=ALTERED_AMOUNT_DATA))
        auth = read_authorized_operation(j1, op)
        verdict = await verify_authorized_operation(auth, HASH, reader, oracle)
        assert verdict.ok, verdict.reason()
        return j1, op, verdict, j2, reader, oracle, {
            "note": "verified against j1, applied against j2 where the same operation id "
                    "denotes a 10x borrow",
            "verified_digest": verdict.envelope_digest,
            "j2_digest": read_authorized_operation(j2, op).computed_digest,
        }

    await refuses_at_apply("apply_cross_journal_different_envelope", build=build_cross_journal)

    # --- cross-journal: same operation id, IDENTICAL envelope -------------
    async def build_cross_journal_same(td: Path):
        oracle = FakeOracle(owned=(HASH,))
        j1, op = new_journal(td, authorized_envelope(), name="k1.sqlite")
        j2, _ = new_journal(td, authorized_envelope(), name="k2.sqlite")
        insert_row(j2, op, authorized_envelope())
        auth = read_authorized_operation(j1, op)
        verdict = await verify_authorized_operation(auth, HASH, reader, oracle)
        assert verdict.ok
        return j1, op, verdict, j2, reader, oracle, {
            "note": "even with a byte-identical envelope, a verdict does not travel between "
                    "journals: it is bound to the file it read"}

    await refuses_at_apply("apply_cross_journal_identical_envelope", build=build_cross_journal_same)

    # --- the envelope changed under the verdict ---------------------------
    async def build_envelope_swapped(td: Path):
        oracle = FakeOracle(owned=(HASH,))
        j, op = new_journal(td, authorized_envelope())
        auth = read_authorized_operation(j, op)
        verdict = await verify_authorized_operation(auth, HASH, reader, oracle)
        assert verdict.ok
        set_envelope(j, op, authorized_envelope(data=ALTERED_AMOUNT_DATA))
        return j, op, verdict, j, reader, oracle, {
            "note": "same journal, same operation id, but the authorization now says something "
                    "else; the digest guard fires before anything is written"}

    await refuses_at_apply("apply_envelope_changed_after_verification", build=build_envelope_swapped)

    # --- ownership revoked between verify and apply -----------------------
    async def build_ownership_revoked(td: Path):
        j, op = new_journal(td, authorized_envelope())
        auth = read_authorized_operation(j, op)
        verdict = await verify_authorized_operation(auth, HASH, reader, FakeOracle(owned=(HASH,)))
        assert verdict.ok
        return j, op, verdict, j, reader, FakeOracle(owned=()), {
            "note": "the verdict was computed against records that bound the hash; at write "
                    "time they do not, and the fresh check decides"}

    await refuses_at_apply("apply_ownership_gone_at_write_time", build=build_ownership_revoked)

    # --- another row claimed the hash in between ---------------------------
    async def build_hash_taken(td: Path):
        oracle = FakeOracle(owned=(HASH,))
        j, op = new_journal(td, authorized_envelope())
        auth = read_authorized_operation(j, op)
        verdict = await verify_authorized_operation(auth, HASH, reader, oracle)
        assert verdict.ok
        insert_row(j, "some-other-operation", authorized_envelope(), state="submitted", txn_hash=HASH)
        return j, op, verdict, j, reader, oracle, {
            "note": "a concurrent resolution took the hash first"}

    await refuses_at_apply("apply_hash_claimed_by_another_row_in_between", build=build_hash_taken)

    # --- the row was settled in between -----------------------------------
    async def build_row_settled(td: Path):
        oracle = FakeOracle(owned=(HASH,))
        j, op = new_journal(td, authorized_envelope())
        auth = read_authorized_operation(j, op)
        verdict = await verify_authorized_operation(auth, HASH, reader, oracle)
        assert verdict.ok
        conn = sqlite3.connect(j, isolation_level=None)
        conn.execute("UPDATE operations SET state='submitted', txn_hash=?, consumed=1"
                     " WHERE operation_id=?", (OTHER_HASH, op))
        conn.close()
        return j, op, verdict, j, reader, oracle, {"note": "resolved by someone else first"}

    await refuses_at_apply("apply_row_settled_in_between", build=build_row_settled)

    # --- the chain changed its mind ---------------------------------------
    async def build_chain_changed(td: Path):
        oracle = FakeOracle(owned=(HASH,))
        j, op = new_journal(td, authorized_envelope())
        auth = read_authorized_operation(j, op)
        verdict = await verify_authorized_operation(auth, HASH, reader, oracle)
        assert verdict.ok
        return j, op, verdict, j, FakeChainReader(tx(input=ALTERED_AMOUNT_DATA)), oracle, {
            "note": "the transaction the node returns at write time is a different call "
                    "(reorg, or a node that lied once)"}

    await refuses_at_apply("apply_chain_disagrees_at_write_time", build=build_chain_changed)


async def structural_tests() -> None:
    """The 'no path around it' properties, asserted rather than asserted-about."""
    out: dict[str, Any] = {"case": "structure_no_bypass_path", "checks": {}}
    gate_py = ROOT / "moonwell_demo" / "gate.py"
    cli_py = ROOT / "scripts" / "operator_resolve.py"
    src = gate_py.read_text()
    tree = ast.parse(src)
    cli_tree = ast.parse(cli_py.read_text())

    def func(tree_: ast.AST, name: str, cls: str | None = None) -> ast.AST | None:
        for node in ast.walk(tree_):
            if cls and isinstance(node, ast.ClassDef) and node.name == cls:
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                        return sub
            if not cls and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        return None

    def params(node) -> list[str]:
        a = node.args
        return [x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]]

    def calls(node) -> set[str]:
        names = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name):
                    names.add(n.func.id)
                elif isinstance(n.func, ast.Attribute):
                    names.add(n.func.attr)
        return names

    c = out["checks"]

    # 1. verification requires an ownership oracle as a positional parameter
    verify = func(tree, "verify_authorized_operation")
    c["verify_takes_a_required_oracle"] = params(verify)[:4] == [
        "authorized", "candidate_hash", "reader", "oracle"]

    # 2. mark_resolved re-verifies at write time and takes what it needs to
    mr = func(tree, "mark_resolved")
    c["mark_resolved_is_async"] = isinstance(mr, ast.AsyncFunctionDef)
    c["mark_resolved_takes_reader_and_oracle"] = params(mr) == [
        "journal_path", "verdict", "reader", "oracle"]
    mr_calls = calls(mr)
    c["mark_resolved_reverifies"] = "verify_authorized_operation" in mr_calls
    c["mark_resolved_rereads_the_journal"] = "read_authorized_operation" in mr_calls
    c["mark_resolved_uses_begin_immediate"] = "BEGIN IMMEDIATE" in ast.get_source_segment(src, mr)

    # 3. the hash written comes from the FRESH verdict, not the one handed in
    body = ast.get_source_segment(src, mr)
    c["written_hash_comes_from_fresh_verdict"] = (
        "txn_hash = fresh.authorized_hash()" in body
        and re.search(r"\(txn_hash, verdict\.operation_id\)", body) is not None
        and "(claimed, verdict.operation_id)" not in body
    )
    c["single_update_site"] = src.count("UPDATE operations") == 1

    # 4. `ok` cannot be true without ownership
    ok_prop = func(tree, "ok", cls="ReconciliationVerdict")
    c["verdict_ok_requires_ownership"] = "ownership_established" in ast.get_source_segment(src, ok_prop)
    c["verdict_ok_requires_seal"] = "seal_valid" in ast.get_source_segment(src, ok_prop)

    # 5. a chain scan can never supply ownership
    owns = func(tree, "owns", cls="OwnershipFinding")
    owns_src = ast.get_source_segment(src, owns)
    c["owns_never_reads_hints"] = "hints" not in owns_src and "self.owned" in owns_src

    # 6. no force/override switch anywhere, and no operator-chosen RPC
    banned = {"force", "yes", "override", "skip", "skip_checks", "ignore",
              "ignore_mismatch", "no_verify", "attest", "trust", "assume_owned"}

    def all_params(t) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(t):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.update(params(node))
        return names

    def cli_flags(t) -> set[str]:
        flags: set[str] = set()
        for node in ast.walk(t):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        flags.add(arg.value.lstrip("-").replace("-", "_"))
        return flags

    c["no_banned_parameter_in_gate"] = not (all_params(tree) & banned)
    c["no_banned_parameter_in_cli"] = not (all_params(cli_tree) & banned)
    flags = cli_flags(cli_tree)
    c["no_banned_flag_in_cli"] = not (flags & banned)
    c["no_operator_chosen_rpc_flag"] = "rpc" not in flags
    out["cli_flags"] = sorted(flags)

    # 7. the CLI's resolve path goes through resolve_operation only
    cli_src = cli_py.read_text()
    c["cli_resolve_uses_resolve_operation"] = (
        "resolve_operation(" in cli_src and "mark_resolved" not in cli_src)

    out["passed"] = all(v for v in c.values())
    RESULTS.append(out)
    print(("PASS " if out["passed"] else "FAIL ") + "structure_no_bypass_path "
          + json.dumps({k: v for k, v in c.items() if not v} or "all true"))


# --------------------------------------------------------------------------
# the live fork + live KeeperHub group
# --------------------------------------------------------------------------


async def real_cases(args) -> None:
    reader = RpcChainReader(args.rpc)
    oracle = KeeperHubOwnershipOracle(
        workflow_id=args.workflow_id, chain_id=CHAIN, wallet_address=SENDER,
        sql=KeeperHubSql(),
    )
    a_env = json.loads(args.real_envelope)

    # 1. the historical operation A, resolved against KeeperHub's real record
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "sdk-journal.sqlite"
        OperationJournal(path).close()
        insert_row(path.resolve(), args.real_operation_id, a_env)
        auth = read_authorized_operation(path.resolve(), args.real_operation_id)
        v = await verify_authorized_operation(auth, args.real_a_hash, reader, oracle)
        wrote = None
        err = None
        try:
            wrote = await mark_resolved(path.resolve(), v, reader, oracle)
        except ReconciliationRefused as exc:
            err = str(exc)
        r = row(path.resolve(), args.real_operation_id)
        record("real_owned_and_matching_resolves", bool(v.ok and wrote and r[0] == "submitted"),
               note="operation A's own hash, traced to KeeperHub's execution record for A's "
                    "operation id, matching all five fields",
               operation_id=args.real_operation_id, candidate=args.real_a_hash,
               ownership_sources=list(v.ownership_sources),
               ownership=v.informational.get("ownership"),
               checks=[c.as_dict() for c in v.checks], wrote=wrote, refusal=err,
               journal_after={"state": r[0], "txn_hash": r[1]})

    # 2. THE REVIEW'S CASE, live: operation B is identical and never broadcast
    with tempfile.TemporaryDirectory() as td:
        path, op_b = new_journal(Path(td), a_env)
        auth = read_authorized_operation(path, op_b)
        v = await verify_authorized_operation(auth, args.real_a_hash, reader, oracle)
        before = row(path, op_b)
        wrote = None
        err = None
        try:
            wrote = await mark_resolved(path, v, reader, oracle)
        except ReconciliationRefused as exc:
            err = str(exc)
        content = [c for c in v.checks if c.field in
                   ("chain_id", "sender", "recipient", "calldata", "value")]
        record("real_review_case_identical_envelope_refused",
               (not v.ok) and wrote is None and row(path, op_b) == before
               and all(c.match for c in content) and "ownership" in v.failed_fields,
               note="operation B: brand-new operation id, byte-identical envelope, never sent "
                    "to KeeperHub. A's real mined receipt-status-1 transaction matches all five "
                    "fields and is refused on ownership",
               operation_id=op_b, candidate=args.real_a_hash,
               all_five_content_checks_matched=all(c.match for c in content),
               failed_fields=v.failed_fields, reason=v.reason(),
               ownership=v.informational.get("ownership"),
               checks=[c.as_dict() for c in v.checks], refusal=err,
               journal_after={"state": row(path, op_b)[0], "txn_hash": row(path, op_b)[1]})

    # 3. a different real transaction from the same wallet
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "sdk-journal.sqlite"
        OperationJournal(path).close()
        insert_row(path.resolve(), args.real_operation_id, a_env)
        auth = read_authorized_operation(path.resolve(), args.real_operation_id)
        v = await verify_authorized_operation(auth, args.real_b_hash, reader, oracle)
        wrote = None
        err = None
        try:
            wrote = await mark_resolved(path.resolve(), v, reader, oracle)
        except ReconciliationRefused as exc:
            err = str(exc)
        rec = (v.informational.get("receipt") or {}).get("status")
        record("real_other_transaction_refused",
               (not v.ok) and wrote is None and row(path.resolve(), args.real_operation_id)[0] == "pending",
               note="a real, mined, receipt-status-1 transaction from the same wallet -- "
                    "exactly what the naive 'the receipt succeeded' check accepts",
               candidate=args.real_b_hash, receipt_status=rec,
               naive_check_would_have_accepted=rec == 1,
               failed_fields=v.failed_fields, reason=v.reason(), refusal=err)

    # 4. THE REVIEW'S ADDITION, live: a real on-chain transaction that matches all
    #    five fields and that KeeperHub has never heard of.
    if args.unowned_hash and args.unowned_envelope:
        env = json.loads(args.unowned_envelope)
        with tempfile.TemporaryDirectory() as td:
            path, op_c = new_journal(Path(td), env)
            auth = read_authorized_operation(path, op_c)
            v = await verify_authorized_operation(auth, args.unowned_hash, reader, oracle)
            before = row(path, op_c)
            wrote = None
            err = None
            try:
                wrote = await mark_resolved(path, v, reader, oracle)
            except ReconciliationRefused as exc:
                err = str(exc)
            content = [c for c in v.checks if c.field in
                       ("chain_id", "sender", "recipient", "calldata", "value")]
            consumed_check = next(c for c in v.checks if c.field == "hash_not_consumed")
            record("real_unowned_onchain_tx_refused_on_ownership_alone",
                   (not v.ok) and wrote is None and row(path, op_c) == before
                   and all(c.match for c in content) and consumed_check.match
                   and v.failed_fields == ["ownership"],
                   note="REQUIRED BY THE REVIEW, live. A transaction broadcast straight to the "
                        "fork, in no journal and in no KeeperHub record. All five content checks "
                        "match, hash_not_consumed PASSES, and the only thing refusing it is "
                        "positive ownership",
                   operation_id=op_c, candidate=args.unowned_hash,
                   all_five_content_checks_matched=all(c.match for c in content),
                   hash_not_consumed_passed=consumed_check.match,
                   failed_fields=v.failed_fields, reason=v.reason(),
                   ownership=v.informational.get("ownership"),
                   checks=[c.as_dict() for c in v.checks], refusal=err)

    # 5. a hash that exists nowhere at all
    with tempfile.TemporaryDirectory() as td:
        path, op = new_journal(Path(td), a_env)
        auth = read_authorized_operation(path, op)
        made_up = "0x" + uuid.uuid4().hex + uuid.uuid4().hex
        v = await verify_authorized_operation(auth, made_up, reader, oracle)
        record("real_invented_hash_refused", not v.ok,
               candidate=made_up, reason=v.reason(),
               blockers=list(v.blockers), failed_fields=v.failed_fields)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "evidence" / "61-gate-tests.json"))
    ap.add_argument("--rpc", default="http://127.0.0.1:8545")
    ap.add_argument("--workflow-id")
    ap.add_argument("--real-operation-id", help="an operation id KeeperHub really executed")
    ap.add_argument("--real-a-hash", help="the transaction KeeperHub bound to that operation")
    ap.add_argument("--real-b-hash", help="a different real transaction from the same sender")
    ap.add_argument("--real-envelope", help="JSON envelope authorizing --real-a-hash")
    ap.add_argument("--unowned-hash", help="a real on-chain tx KeeperHub never executed")
    ap.add_argument("--unowned-envelope", help="JSON envelope matching --unowned-hash")
    args = ap.parse_args()

    print("--- content: the five required negatives still refuse ---")
    await content_cases()
    print("\n--- ownership: content match is not ownership (defect 1) ---")
    await ownership_cases()
    print("\n--- verdict integrity (defect 2) ---")
    await mutation_cases()
    print("\n--- apply-time re-verification (defect 3) ---")
    await apply_time_cases()
    print("\n--- structure ---")
    await structural_tests()

    if args.real_a_hash and args.real_envelope and args.workflow_id and args.real_operation_id:
        print("\n--- live fork + live KeeperHub records ---")
        await real_cases(args)

    passed = sum(1 for r in RESULTS if r.get("passed"))
    payload = {
        "gate": "moonwell_demo/gate.py revision 2 (ownership + content)",
        "total": len(RESULTS),
        "passed": passed,
        "all_passed": passed == len(RESULTS),
        "results": RESULTS,
    }
    Path(args.out).write_text(json.dumps(payload, indent=1, default=str))
    print(f"\n{passed}/{len(RESULTS)} passed -> {args.out}")
    sys.exit(0 if passed == len(RESULTS) else 1)



if __name__ == "__main__":
    asyncio.run(main())
