"""Assemble one run into the facts the screen shows.

The shape of the answer is fixed by what an operator has to decide:

1. which run this is, and which iteration it reached;
2. per money step and per operation, the three-valued outcome and its hash;
3. where execution stopped and why, in words that name an action;
4. what is needed to continue, concretely.

Two rules are enforced here rather than in the renderer, because they are about
what may be believed rather than what may be drawn:

- ownership comes from the journal operation bound to a step, never from chain
  state. Corroboration is carried alongside a step and is not consulted;
- mode labels come from the run's own manifest and only when that manifest names
  this state directory. Nothing is defaulted, least of all to mainnet.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .actions import Action, CHECK_STATE
from .outcome import JournalOutcomeReader, Outcome, OutcomeFinding, OutcomeReader
from .provenance import Fact, Provenance, inferred, observed, source, unverified
from .sources import CheckpointSnapshot, JournalSnapshot, ManifestSnapshot, StateDirectory, checkpoint_problems

ITERATION_RE = re.compile(r"/iteration/(\d+)(?:/|$)")

#: Mode labels this project keeps distinct, in the order an operator reads them.
MODE_KEYS: tuple[tuple[str, str], ...] = (
    ("chain", "Chain"),
    ("signer", "Signer"),
    ("keeperhub", "KeeperHub"),
    ("sdk_key_material", "SDK key material"),
    ("executor", "Executor"),
    ("seam", "Seam"),
    ("strategy_code", "Strategy code"),
    ("adapters", "Adapters"),
    ("swap_quote", "Swap quote"),
    ("token_metadata_and_prices", "Token metadata and prices"),
    ("steth_apr", "stETH APR"),
    ("funding", "Funding"),
)

#: The four an unlabelled run must still be shown to be missing.
REQUIRED_MODE_KEYS = ("chain", "signer", "keeperhub", "sdk_key_material")

UNDECLARED = "UNDECLARED"

MODE_NOTE = (
    "An undeclared mode is not a mainnet mode and not a real-signer mode. This "
    "screen never supplies a label the run did not record, and never carries a "
    "label from one run to another."
)


class StepStatus(str, Enum):
    EXECUTED = "executed"
    UNKNOWN = "outcome unknown"
    NOT_BOUND = "no operation bound"
    NOT_SENT = "never sent"
    INCOMPLETE = "sent, result not recorded"
    NOT_STARTED = "not started"


@dataclass(frozen=True)
class Need:
    """One concrete thing that has to happen before the run can continue."""

    text: str
    action: Action | None = None
    subject: str = ""
    severity: str = "information"  # information | stop | uncertain


@dataclass(frozen=True)
class StopReport:
    headline: str
    paragraphs: list[str]
    blocked: bool
    status: str = "stopped"  # clear | stopped | uncertain; blocked includes uncertainty


@dataclass(frozen=True)
class OperationView:
    operation_id: str
    state: str
    consumed: bool
    txn_hash: str | None
    envelope: dict[str, Any]
    digest: str
    created_at: float
    sequence: int
    step_id: str | None
    finding: OutcomeFinding
    facts: list[Fact]
    needs: list[Need]

    @property
    def short(self) -> str:
        return self.operation_id[:12]

    @property
    def selector(self) -> str:
        return (self.envelope.get("data") or "0x")[:10]


@dataclass(frozen=True)
class Corroboration:
    label: str
    value: str
    origin: str


@dataclass(frozen=True)
class StepView:
    key: str
    name: str
    iteration: int | None
    checkpoint_state: str | None
    step_id: str
    operation: OperationView | None
    status: StepStatus
    facts: list[Fact]
    corroboration: list[Corroboration]
    needs: list[Need]
    order: tuple[int, Any]


@dataclass(frozen=True)
class RunView:
    state_dir: str
    available: bool
    fixture_notice: str
    source_facts: list[Fact]
    mode_facts: list[Fact]
    mode_note: str
    not_proven: list[str]
    run_facts: list[Fact]
    steps: list[StepView]
    operations: list[OperationView]
    stop: StopReport
    needs: list[Need]
    unattached_corroboration: list[Corroboration]
    problems: list[str]
    probed: list[str]
    reader_name: str
    reader_caveat: str
    checked_at: float

    def all_facts(self) -> list[Fact]:
        """Every fact the page shows, so 'all of them are tagged' is checkable."""
        out = [*self.source_facts, *self.mode_facts, *self.run_facts, *CONTRACT_NOTES]
        for step in self.steps:
            out.extend(step.facts)
        for operation in self.operations:
            out.extend(operation.facts)
        return out


# --------------------------------------------------------------------------
# ownership
# --------------------------------------------------------------------------

def step_executed(
    call_row: dict[str, Any] | None,
    operation: OperationView | None,
    corroboration: list[Corroboration],
) -> bool:
    """Only the journal operation bound to this exact step may mark it executed.

    Chain corroboration is shown next to a step and is deliberately not read
    here. An allowance that looks right does not prove our approve landed, and
    an effect produced by somebody else's identical transaction is still an
    effect. Ownership comes from the binding in ``execution_steps`` and from the
    state of the operation it names, or it does not come at all.
    """
    del corroboration  # named so its exclusion is greppable, not accidental
    return bool(
        operation is not None
        and operation.finding.outcome is Outcome.LANDED
        and call_row is not None
        and call_row.get("state") == "done"
    )


def classify_step(
    call_row: dict[str, Any] | None,
    operation: OperationView | None,
    corroboration: list[Corroboration],
) -> StepStatus:
    if step_executed(call_row, operation, corroboration):
        return StepStatus.EXECUTED
    if operation is None:
        return StepStatus.NOT_BOUND if call_row is not None else StepStatus.NOT_STARTED
    if operation.finding.outcome is Outcome.NEVER_SEEN:
        return StepStatus.NOT_SENT
    if operation.finding.outcome is Outcome.INDETERMINATE:
        return StepStatus.UNKNOWN
    return StepStatus.INCOMPLETE


# --------------------------------------------------------------------------
# mode labels
# --------------------------------------------------------------------------

def mode_facts(manifest: ManifestSnapshot | None, fixture_notice: str) -> list[Fact]:
    """Mode labels, or a visible statement that the run recorded none."""
    prefix: list[Fact] = []
    if fixture_notice:
        prefix.append(
            Fact(
                "Data", "FIXTURE — synthetic, written by operator_console.fixture",
                Provenance.OBSERVED, "FIXTURE.txt in the state directory",
                "Not a run. No chain, no signer, no KeeperHub, nothing broadcast.",
            )
        )
    return prefix + _declared_modes(manifest)


def _declared_modes(manifest: ManifestSnapshot | None) -> list[Fact]:
    if manifest is None:
        return _undeclared_modes(
            "no run manifest was supplied, so this run's mode labels are unknown"
        )
    if not manifest.bound:
        return _undeclared_modes(manifest.binding_detail)
    labels = manifest.mode_labels
    if not labels:
        return _undeclared_modes(
            f"{manifest.path.name} describes this run but records no mode_labels"
        )
    facts: list[Fact] = []
    seen: set[str] = set()
    for key, label in MODE_KEYS:
        if key in labels:
            seen.add(key)
            facts.append(observed(label, labels[key], f"{manifest.path.name}:mode_labels.{key}"))
    for key in sorted(set(labels) - seen):
        facts.append(observed(key, labels[key], f"{manifest.path.name}:mode_labels.{key}"))
    for key in REQUIRED_MODE_KEYS:
        if key not in labels:
            facts.append(
                unverified(
                    dict(MODE_KEYS)[key], UNDECLARED, f"{manifest.path.name}:mode_labels",
                    "the manifest describes this run but does not record this label",
                )
            )
    return facts


def _undeclared_modes(reason: str) -> list[Fact]:
    return [
        unverified(label, UNDECLARED, "no bound run manifest", reason)
        for key, label in MODE_KEYS
        if key in REQUIRED_MODE_KEYS
    ]


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def build_view(
    state: StateDirectory,
    reader: OutcomeReader | None = None,
    *,
    now: float | None = None,
) -> RunView:
    reader = reader or JournalOutcomeReader()
    checked_at = now if now is not None else time.time()
    problems = list(state.problems)

    source_facts = _source_facts(state)
    modes = mode_facts(state.manifest, state.fixture_notice)
    not_proven = state.manifest.not_proven if state.manifest and state.manifest.bound else []

    if state.journal is None:
        return RunView(
            state_dir=str(state.path),
            available=False,
            fixture_notice=state.fixture_notice,
            source_facts=source_facts,
            mode_facts=modes,
            mode_note=MODE_NOTE,
            not_proven=not_proven,
            run_facts=[],
            steps=[],
            operations=[],
            stop=StopReport(
                "No journal, so nothing is known about this run.",
                [
                    "The SDK journal is the record of every operation the SDK started, "
                    "and it is written before the executor is called. Without it this "
                    "screen has nothing to report and will not guess.",
                ],
                blocked=True,
                status="uncertain",
            ),
            needs=[
                Need(
                    "Point this console at a state directory that holds "
                    f"sdk-journal.sqlite. Probed: {', '.join(state.probed) or 'nothing'}.",
                ),
                Need(
                    "A run produces one: moonwell/scripts/run_loop.py --state-dir DIR "
                    "(fork and dev-signer; see docs/reproduction.md). For a labelled, "
                    "obviously synthetic one, run "
                    "`python -m operator_console --make-fixture DIR`.",
                ),
            ],
            unattached_corroboration=[],
            problems=problems,
            probed=list(state.probed),
            reader_name=reader.name,
            reader_caveat=reader.caveat,
            checked_at=checked_at,
        )

    journal = state.journal
    evidence_problems = checkpoint_problems(state.checkpoint, journal)
    evidence_problems.extend(state.problems)
    checkpoint = state.checkpoint if not evidence_problems else None
    problems.extend(p for p in evidence_problems if p not in problems)

    operations = [_operation_view(row, journal, reader) for row in journal.operations]
    by_id = {o.operation_id: o for o in operations}

    corroboration_map = (
        state.manifest.corroboration if state.manifest and state.manifest.bound else {}
    )
    steps = _step_views(checkpoint, journal, by_id, corroboration_map)

    run_facts = _run_facts(journal, checkpoint, state.manifest)
    stop, needs = _stop_and_needs(operations, steps, checkpoint, journal, checked_at)
    if evidence_problems:
        stop = StopReport(
            "Run state is unverified: checkpoint/journal evidence is unavailable or inconsistent.",
            evidence_problems + ["Journal operations below are independent observations; "
                "they do not establish driver step completion. Check state before any continuation."],
            blocked=True, status="uncertain",
        )
        needs = [Need(p, CHECK_STATE) for p in evidence_problems] + needs

    unattached = _unattached_corroboration(state.manifest)

    return RunView(
        state_dir=str(state.path),
        available=True,
        fixture_notice=state.fixture_notice,
        source_facts=source_facts,
        mode_facts=modes,
        mode_note=MODE_NOTE,
        not_proven=not_proven,
        run_facts=run_facts,
        steps=steps,
        operations=operations,
        stop=stop,
        needs=needs,
        unattached_corroboration=unattached,
        problems=problems,
        probed=list(state.probed),
        reader_name=reader.name,
        reader_caveat=reader.caveat,
        checked_at=checked_at,
    )


def _source_facts(state: StateDirectory) -> list[Fact]:
    facts: list[Fact] = []
    if state.journal is not None:
        facts.append(observed("Journal", state.journal.path, "the file this process opened"))
        facts.append(
            observed(
                "Journal snapshot sha256", state.journal.sha256,
                "operator-console-journal-v1: canonical logical projection",
                "operations (all selected columns, parsed envelope, consumed, sequence), "
                "execution_steps bindings, moonwell_run identity and table names; "
                "one read transaction, not a whole SQLite file/database hash",
            )
        )
        facts.append(
            observed("Journal tables", ", ".join(state.journal.tables), "sqlite_master")
        )
    else:
        facts.append(unverified("Journal", "absent", "state directory", "; ".join(state.problems)))
    if state.checkpoint is not None:
        facts.append(observed("Checkpoint", state.checkpoint.path, "the file this process opened"))
        facts.append(
            observed(
                "Checkpoint integrity", state.checkpoint.integrity,
                "recomputed from the checkpoint's own sha256 field",
            )
        )
    else:
        facts.append(
            unverified(
                "Checkpoint", "absent", "state directory",
                "without it the ordered money steps cannot be shown, only the operations",
            )
        )
    if state.manifest is not None:
        facts.append(observed("Run manifest", state.manifest.path, "the file this process opened"))
        facts.append(
            (observed if state.manifest.bound else unverified)(
                "Manifest binding",
                "describes this run" if state.manifest.bound else "refused",
                f"{state.manifest.path.name}:stateDir",
                state.manifest.binding_detail,
            )
        )
    else:
        facts.append(
            unverified(
                "Run manifest", "none supplied", "command line",
                "mode labels can only come from a manifest that names this state directory",
            )
        )
    facts.append(source(
        "Cross-file consistency", "Not atomic",
        "console/operator_console/sources.py:load_state_directory",
        "JSON and SQLite are read separately. Detected contradictions refuse driver progress; "
        "agreement cannot prove simultaneous capture or permission to continue.",
    ))
    return facts


def _run_facts(
    journal: JournalSnapshot,
    checkpoint: CheckpointSnapshot | None,
    manifest: ManifestSnapshot | None,
) -> list[Fact]:
    facts: list[Fact] = []
    if checkpoint is not None:
        facts.append(
            observed("Strategy", checkpoint.strategy or UNDECLARED, "run-plan.json:data.strategy")
        )
        facts.append(observed("Run id", checkpoint.run_id or UNDECLARED, "run-plan.json:data.run_id"))
    else:
        facts.append(
            unverified(
                "Strategy", UNDECLARED, "no driver checkpoint",
                "the journal records operations, not which strategy asked for them",
            )
        )
    facts.append(
        (observed if journal.driver_run_id else unverified)(
            "Run id bound in the journal",
            journal.driver_run_id or "none",
            "sdk-journal.sqlite:moonwell_run.run_id",
            "" if journal.driver_run_id else "this journal carries no driver run binding",
        )
    )
    if checkpoint is not None:
        iterations = checkpoint.iterations
        done = sum(1 for r in iterations if r.get("status") == "done")
        in_flight = checkpoint.in_flight
        facts.append(
            observed(
                "Iterations", f"{done} done of {len(iterations)} recorded",
                "run-plan.json:data.iterations[].status",
            )
        )
        if in_flight is not None:
            facts.append(
                observed(
                    "Current iteration",
                    f"#{in_flight.get('index')} ({in_flight.get('status')})",
                    "run-plan.json:data.iterations[] first not done",
                )
            )
            if isinstance(in_flight.get("borrow_amt_wei"), int):
                facts.append(
                    observed(
                        "Iteration intent", f"borrow {in_flight['borrow_amt_wei']} wei",
                        "run-plan.json:data.iterations[].borrow_amt_wei",
                        "the amount this iteration committed to before it started",
                    )
                )
        seed = checkpoint.data.get("seed")
        if isinstance(seed, dict):
            facts.append(observed("Seed", seed.get("state", UNDECLARED), "run-plan.json:data.seed.state"))
    senders = sorted({r["sender"] for r in journal.operations})
    chains = sorted({str(r["chain_id"]) for r in journal.operations})
    facts.append(
        observed("Sender", ", ".join(senders) or "—", "sdk-journal.sqlite:operations.sender")
    )
    facts.append(
        observed("Chain id", ", ".join(chains) or "—", "sdk-journal.sqlite:operations.chain_id")
    )
    facts.append(
        observed("Operations recorded", len(journal.operations), "sdk-journal.sqlite:operations")
    )
    if manifest is not None and manifest.bound:
        for key, label in (("workflowId", "KeeperHub workflow"), ("wallet", "Wallet"),
                           ("keeperhub", "KeeperHub base"), ("rpc", "RPC")):
            if key in manifest.data:
                facts.append(observed(label, manifest.data[key], f"{manifest.path.name}:{key}"))
    return facts


def _operation_view(
    row: dict[str, Any], journal: JournalSnapshot, reader: OutcomeReader
) -> OperationView:
    finding = reader.finding(row)
    step_id = journal.step_for_operation(row["operation_id"])
    envelope = row["envelope"]
    facts = [
        observed("Operation id", row["operation_id"], "sdk-journal.sqlite:operations.operation_id"),
        Fact(
            "Outcome", finding.outcome.value, finding.provenance, finding.origin, finding.detail
        ),
        (observed if row["txn_hash"] else unverified)(
            "Transaction hash", row["txn_hash"] or "none recorded",
            "sdk-journal.sqlite:operations.txn_hash",
            "" if row["txn_hash"] else "no hash is bound to this operation in the journal",
        ),
        observed("Journal state", row["state"], "sdk-journal.sqlite:operations.state"),
        observed("Consumed", row["consumed"], "sdk-journal.sqlite:operations.consumed",
                 "whether the hash was ever handed back to the calling code"),
        observed("To", envelope.get("to"), "sdk-journal.sqlite:operations.envelope.to"),
        observed("Value (wei)", envelope.get("value"), "sdk-journal.sqlite:operations.envelope.value"),
        observed("Selector", (envelope.get("data") or "0x")[:10],
                 "sdk-journal.sqlite:operations.envelope.data[:10]"),
        observed("Envelope digest", row["digest"], "sdk-journal.sqlite:operations.digest",
                 "the SDK's pre-send authorization, written before the executor was called"),
        (observed if step_id else unverified)(
            "Bound driver step", step_id or "none",
            "sdk-journal.sqlite:execution_steps.step_id",
            "" if step_id else "no driver step claims this operation",
        ),
    ]
    return OperationView(
        operation_id=row["operation_id"],
        state=row["state"],
        consumed=row["consumed"],
        txn_hash=row["txn_hash"],
        envelope=envelope,
        digest=row["digest"],
        created_at=row["created_at"],
        sequence=row.get("seq", 0),
        step_id=step_id,
        finding=finding,
        facts=facts,
        needs=_operation_needs(row, finding),
    )


def _operation_needs(row: dict[str, Any], finding: OutcomeFinding) -> list[Need]:
    op = row["operation_id"]
    if finding.outcome is Outcome.INDETERMINATE:
        return [
            Need(
                f"Operation {op} has an unknown fate. Check its state: re-run the "
                "read-only reconciliation and ask the executor's lookup about this "
                "operation id. It can continue only when that answers landed (the "
                "seam adopts the hash) or never_seen (the caller may send again).",
                CHECK_STATE, op,
            ),
            Need(
                "If lookup cannot settle it, an operator resolves it only with a hash "
                "KeeperHub's own record binds to this operation id, through "
                "moonwell/scripts/operator_resolve.py verify then resolve. That command "
                "writes; this screen does not.",
                None, op,
            ),
        ]
    if row["state"] == "orphaned":
        return [
            Need(
                f"Operation {op} landed as {row['txn_hash']} but a newer operation began "
                "before anything claimed it, so it is orphaned. It needs out-of-band "
                "reconciliation before this run's accounting is complete.",
                None, op,
            )
        ]
    if row["state"] == "submitted" and not row["consumed"]:
        return [
            Need(
                f"Operation {op} has a hash the calling code never received. The next "
                "identical send from this sender claims it instead of sending again; "
                "nothing has to be done here.",
                None, op,
            )
        ]
    return []


def _step_views(
    checkpoint: CheckpointSnapshot | None,
    journal: JournalSnapshot,
    by_id: dict[str, OperationView],
    corroboration_map: dict[str, list[dict[str, Any]]],
) -> list[StepView]:
    if checkpoint is None:
        return []
    steps: list[StepView] = []
    for key, row in checkpoint.calls.items():
        if not isinstance(row, dict) or not row.get("money") or row.get("composite"):
            continue
        step_id = f"{key}/send/0"
        operation = by_id.get(journal.step_bindings.get(step_id, ""))
        corroboration = [
            Corroboration(
                str(entry.get("label", "observation")),
                str(entry.get("value", "")),
                str(entry.get("source", "run manifest:corroboration")),
            )
            for entry in corroboration_map.get(key, [])
        ]
        status = classify_step(row, operation, corroboration)
        iteration = _iteration_of(key)
        facts = [
            observed("Driver call", key, "run-plan.json:data.calls"),
            observed("Checkpoint state", row.get("state"), "run-plan.json:data.calls[].state"),
            (observed if operation else unverified)(
                "Bound operation",
                operation.operation_id if operation else "none",
                "sdk-journal.sqlite:execution_steps",
                "" if operation else
                "no operation is bound to this step id, so nothing owns this step",
            ),
            inferred(
                "Step state", status.value,
                "derived from the bound operation's journal state only",
                "chain effects are never allowed to mark a step executed",
            ),
        ]
        if operation is not None:
            facts.append(
                Fact("Outcome", operation.finding.outcome.value, operation.finding.provenance,
                     operation.finding.origin, operation.finding.detail)
            )
        steps.append(
            StepView(
                key=key,
                name=_step_name(key),
                iteration=iteration,
                checkpoint_state=row.get("state"),
                step_id=step_id,
                operation=operation,
                status=status,
                facts=facts,
                corroboration=corroboration,
                needs=_step_needs(_step_name(key), key, status, operation, corroboration),
                order=(0, operation.sequence) if operation else (1, key),
            )
        )
    steps.sort(key=lambda s: s.order)
    return steps


def _step_needs(
    name: str,
    key: str,
    status: StepStatus,
    operation: OperationView | None,
    corroboration: list[Corroboration],
) -> list[Need]:
    needs: list[Need] = []
    if status is StepStatus.UNKNOWN and operation is not None:
        needs.append(
            Need(
                f"This step's operation ({operation.operation_id}) has an unknown fate; "
                "check its state.",
                CHECK_STATE, name,
            )
        )
    if status is StepStatus.NOT_BOUND:
        needs.append(
            Need(
                "The driver recorded this call but no journal operation is bound to its "
                f"step id ({key}/send/0). Nothing owns it, so it cannot be treated as done.",
                None, name,
            )
        )
        if corroboration:
            needs.append(
                Need(
                    "Chain state next to this step looks consistent with the effect. That "
                    "is corroboration, not ownership: an identical transaction from anyone "
                    "produces the same effect. It does not make this step done.",
                    None, name,
                )
            )
    if status is StepStatus.INCOMPLETE and operation is not None:
        needs.append(
            Need(
                f"The operation for this step landed as {operation.txn_hash} but the driver "
                "did not record its result. Resuming re-enters this step with the same "
                "identity and adopts that hash; it does not send again.",
                None, name,
            )
        )
    return needs


def _iteration_of(key: str) -> int | None:
    match = ITERATION_RE.search(key)
    return int(match.group(1)) if match else None


def _step_name(key: str) -> str:
    """Name a step by the driver's own call path, not by a guess at intent."""
    parts = key.split("/")
    if "iteration" in parts:
        tail = parts[parts.index("iteration") + 2:]
    else:
        tail = parts[1:]
    tail = [p for p in tail if p != "0"]
    return " › ".join(tail) or key


def _stop_and_needs(
    operations: list[OperationView],
    steps: list[StepView],
    checkpoint: CheckpointSnapshot | None,
    journal: JournalSnapshot,
    now: float,
) -> tuple[StopReport, list[Need]]:
    needs: list[Need] = []
    paragraphs: list[str] = []

    status = "stopped"
    unknown = [o for o in operations if o.finding.outcome is Outcome.INDETERMINATE]
    orphaned = [o for o in operations if o.state == "orphaned"]
    stopped_steps = [s for s in steps if s.status is not StepStatus.EXECUTED]

    if unknown:
        first = unknown[0]
        where = next((s for s in steps if s.operation and s.operation.operation_id == first.operation_id), None)
        headline = (
            f"Execution stopped at {where.name}." if where
            else "Execution stopped at an operation whose fate is unknown."
        )
        paragraphs.append(
            f"Operation {first.operation_id} is still pending in the journal. The SDK "
            "wrote its authorization before the executor was called, then never recorded "
            "a hash for it. It may be on chain and it may not be."
        )
        paragraphs.append(
            "While that is unknown the seam refuses every new send for "
            f"{first.envelope.get('from')} on chain {first.envelope.get('chainId')}. That "
            "refusal is the safe behaviour, not a failure: sending again would double "
            "send if the first one landed."
        )
        for operation in unknown:
            needs.extend(operation.needs)
    elif orphaned:
        headline = "A landed operation is orphaned and needs reconciliation."
        paragraphs.append(
            "The run moved on to a newer operation before this one was claimed, so its "
            "hash is real but not bound to a step of this run."
        )
        for operation in orphaned:
            needs.extend(operation.needs)
    elif stopped_steps:
        first = stopped_steps[0]
        headline = f"Execution has not completed {first.name}."
        paragraphs.append(
            f"The driver recorded this call in state {first.checkpoint_state!r} and its "
            f"step is {first.status.value}."
        )
    else:
        status = "clear"
        headline = "No stop is recorded in the available evidence."
        paragraphs.append(
            "Every money step in the checkpoint is owned by a journal operation with a "
            "hash, and no operation is pending. That is not a statement that the strategy "
            "achieved its goal: a landed hash is a transaction the executor produced for "
            "an authorized operation, and the SDK verifies each receipt separately."
        )

    for step in steps:
        needs.extend(step.needs)
    if checkpoint is not None:
        quote_needs = _quote_needs(checkpoint, journal, now)
        needs.extend(quote_needs)
        if unknown or orphaned:
            paragraphs.extend(n.text for n in quote_needs)
        unfinished = [(k, r) for k, r in checkpoint.calls.items()
                      if r.get("composite") and r["state"] != "done"]
        seed = checkpoint.data.get("seed")
        seed_pending = isinstance(seed, dict) and seed.get("state") == "intent"
        iteration = checkpoint.in_flight
        if not unknown and not orphaned:
            if quote_needs:
                status = "stopped" if any(n.severity == "stop" for n in quote_needs) else "uncertain"
                headline = ("Execution cannot continue with the saved quote." if status == "stopped"
                            else "Continuation from the saved quote is unverified.")
                paragraphs = [n.text for n in quote_needs]
            elif seed_pending:
                headline = "Seed is unfinished; continuation requires reconciliation."
                paragraphs = ["run-plan.json:data.seed.state is intent, not done or external. "
                              "No completed seed is inferred from balances or absence of pending operations."]
                needs.append(Need(paragraphs[0], CHECK_STATE, "seed"))
                status = "stopped"
            elif unfinished or iteration:
                detail = (f"Composite call {unfinished[0][0]} is {unfinished[0][1]['state']}. "
                          if unfinished else "")
                detail += (f"Iteration #{iteration['index']} is {iteration['status']}. " if iteration else "")
                detail += "Source: run-plan.json:data.calls / data.iterations. "
                if not stopped_steps:
                    headline = ("Execution stopped before completing the recorded iteration."
                                if iteration and iteration['status'] == 'stopped' else
                                "Recorded execution is incomplete; current activity is unverified.")
                    paragraphs = [detail + "No pending operation does not mean the strategy finished. "
                                  "An in-flight checkpoint alone cannot distinguish a running writer from an interruption."]
                needs.append(Need(detail + "Check state; this screen does not continue execution.", CHECK_STATE))
                status = "stopped" if iteration and iteration['status'] == 'stopped' else "uncertain"
    else:
        if not unknown and not orphaned:
            headline = "Driver progress is unverified without a usable checkpoint."
            status = "uncertain"
            paragraphs = ["Only journal operations can be reported."]

    deduped: list[Need] = []
    seen: set[str] = set()
    for need in needs:
        if need.text not in seen:
            seen.add(need.text)
            deduped.append(need)

    return StopReport(headline, paragraphs, blocked=status != "clear", status=status), deduped


def _quote_needs(checkpoint: CheckpointSnapshot, journal: JournalSnapshot, now: float) -> list[Need]:
    """The saved-quote boundary the driver enforces, restated for a person."""
    needs: list[Need] = []
    for key, row in checkpoint.calls.items():
        if not key.endswith("/best_quote") or not isinstance(row, dict):
            continue
        parent = key.rsplit("/", 1)[0]
        swap_step = f"{parent}/swap_from_quote/transaction/send/0"
        if swap_step in journal.step_bindings:
            continue
        if row.get("state") != "done":
            needs.append(Need(
                f"The quote request was interrupted at {key} with no saved result. "
                "The driver refuses an automatic second quote request.", CHECK_STATE, key, "stop"))
            continue
        expiry = row.get("expires_at")
        if expiry is None:
            needs.append(
                Need(
                    f"The saved quote at {key} records no expiry and no swap operation is "
                    "bound yet. On reopening, the driver stops before approving or swapping; "
                    "the saved files do not establish whether that reopening happened. A new quote is a separate operator "
                    "decision; nothing here requests one.",
                    None, key, "uncertain",
                )
            )
        elif isinstance(expiry, (int, float)) and expiry <= now:
            needs.append(
                Need(
                    f"The saved quote at {key} expired and no swap operation is bound to "
                    "it. The driver stops rather than reusing it or fetching another. A "
                    "new quote is a separate operator decision.",
                    None, key, "stop",
                )
            )
    return needs


def _unattached_corroboration(manifest: ManifestSnapshot | None) -> list[Corroboration]:
    """Chain readings a run recorded that belong to no single step."""
    if manifest is None or not manifest.bound:
        return []
    out: list[Corroboration] = []
    position = manifest.position
    if position:
        for key in ("block", "nonce"):
            if key in position:
                out.append(
                    Corroboration(key, str(position[key]), f"{manifest.path.name}:position.{key}")
                )
        for group in ("wallet", "moonwell"):
            values = position.get(group)
            if isinstance(values, dict):
                for name, value in values.items():
                    out.append(
                        Corroboration(
                            f"{group}.{name}", str(value),
                            f"{manifest.path.name}:position.{group}.{name}",
                        )
                    )
    if "fork_block" in manifest.data:
        out.append(
            Corroboration(
                "fork_block", str(manifest.data["fork_block"]), f"{manifest.path.name}:fork_block"
            )
        )
    return out


CONTRACT_NOTES: list[Fact] = [
    source(
        "Lookup is three-valued",
        "landed(hash) / never_seen() / indeterminate(detail)",
        "docs/architecture.md §2.2",
        "A two-valued answer conflates 'the executor never saw it' with 'it broadcast "
        "then died before recording'. Reading the second as the first is a double send.",
    ),
    source(
        "never_seen is a strong claim",
        "only an executor that persists the id before it can reach the chain may assert it",
        "docs/architecture.md §2.2",
    ),
    source(
        "Ownership is not chain state",
        "a hash belongs to an operation only through the record bound to that operation id",
        "docs/architecture.md §4.1",
        "Identical operations produce identical envelopes by construction, so a content "
        "match can never establish ownership.",
    ),
    source(
        "Absence of evidence is not evidence of absence",
        "an unchanged nonce does not prove nothing was sent",
        "docs/architecture.md §3.3",
    ),
]
