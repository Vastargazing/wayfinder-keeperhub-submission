"""What the screen is allowed to believe, and what it must refuse to conclude."""

import json
import re

import pytest

import runbuilder
from operator_console.model import (
    Corroboration,
    StepStatus,
    build_view,
    classify_step,
    step_executed,
)
from operator_console.outcome import JournalOutcomeReader, Outcome
from operator_console.provenance import Fact, Provenance
from operator_console.render import render_page
from operator_console.sources import load_state_directory


# --------------------------------------------------------------------------
# the three-valued contract
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "row,expected",
    [
        ({"state": "submitted", "txn_hash": "0x" + "a" * 64}, Outcome.LANDED),
        ({"state": "orphaned", "txn_hash": "0x" + "b" * 64}, Outcome.LANDED),
        ({"state": "never_seen", "txn_hash": None}, Outcome.NEVER_SEEN),
        ({"state": "pending", "txn_hash": None}, Outcome.INDETERMINATE),
        ({"state": "submitted", "txn_hash": None}, Outcome.INDETERMINATE),
        ({"state": "something-new", "txn_hash": None}, Outcome.INDETERMINATE),
    ],
)
def test_outcomes_are_three_valued(row, expected):
    finding = JournalOutcomeReader().finding(row)
    assert finding.outcome is expected
    assert finding.detail
    assert finding.origin
    if expected is Outcome.LANDED:
        assert finding.txn_hash == row["txn_hash"]
    else:
        assert finding.txn_hash is None


def test_a_pending_row_is_indeterminate_not_never_seen(view):
    """The distinction the whole project exists for, on the screen."""
    pending = [o for o in view.operations if o.state == "pending"]
    assert pending
    for operation in pending:
        assert operation.finding.outcome is Outcome.INDETERMINATE
        assert operation.finding.provenance is Provenance.INFERRED
        assert "never have been" in operation.finding.detail


def test_the_stop_report_names_the_step_and_the_refusal(view):
    assert "ensure-allowance" in view.stop.headline
    joined = " ".join(view.stop.paragraphs)
    assert "refuses every new send" in joined
    assert "double" in joined
    assert view.stop.blocked is True


# --------------------------------------------------------------------------
# ownership: chain state is corroboration, never proof
# --------------------------------------------------------------------------

def test_chain_state_cannot_mark_a_step_done(view):
    """An effect that looks right does not make a step this run owns."""
    lend = next(s for s in view.steps if s.name == "lend")
    assert lend.operation is None
    assert lend.corroboration, "the fixture attaches a chain reading to this step"
    assert lend.status is StepStatus.NOT_BOUND
    reasons = " ".join(n.text for n in lend.needs)
    assert "no journal operation is bound" in reasons
    assert "corroboration, not ownership" in reasons


def test_step_executed_ignores_corroboration_directly():
    corroboration = [Corroboration("allowance", "999", "a chain read")]
    call = {"state": "done"}
    assert step_executed(call, None, corroboration) is False
    assert classify_step(call, None, corroboration) is StepStatus.NOT_BOUND


def test_corroboration_is_labelled_as_corroboration_on_the_page(page):
    assert "CORROBORATION, not proof of ownership" in page
    assert "does not prove our operation" in page
    assert "unchanged nonce likewise does not prove nothing was sent" in page


def test_an_executed_step_needs_both_a_landed_operation_and_a_recorded_result(view):
    executed = [s for s in view.steps if s.status is StepStatus.EXECUTED]
    assert executed
    for step in executed:
        assert step.operation is not None
        assert step.operation.finding.outcome is Outcome.LANDED
        assert step.checkpoint_state == "done"


# --------------------------------------------------------------------------
# the run, the ordering and the needs
# --------------------------------------------------------------------------

def test_the_run_its_iteration_and_its_ordered_money_steps(view):
    values = {f.label: f.value for f in view.run_facts}
    assert values["Strategy"] == "moonwell-wsteth-loop"
    assert values["Run id"] == runbuilder.RUN_ID
    assert values["Run id bound in the journal"] == runbuilder.RUN_ID
    assert values["Current iteration"] == "#2 (in_flight)"
    assert [s.name for s in view.steps] == [
        "borrow", "wrap_eth", "ensure-allowance › approve", "lend",
    ]
    assert all(s.iteration == 2 for s in view.steps)


def test_a_disagreeing_checkpoint_and_journal_are_reported(tmp_path):
    directory = runbuilder.write_run(tmp_path / "run")
    plan = json.loads((directory / "run-plan.json").read_text())
    plan["data"]["run_id"] = "b" * 32
    (directory / "run-plan.json").write_text(json.dumps(plan))
    view = build_view(load_state_directory(directory))
    assert any("name different runs" in p for p in view.problems)
    assert "Read problems" in render_page(view)


def test_an_expired_saved_quote_without_a_bound_swap_is_a_need(tmp_path):
    directory = runbuilder.write_run(tmp_path / "run", quote={"expires_at": 1.0})
    view = build_view(load_state_directory(directory))
    texts = " ".join(n.text for n in view.needs)
    assert "expired and no swap operation is bound" in texts
    assert "separate operator decision" in texts


def test_a_quote_with_unknown_validity_is_a_need(tmp_path):
    directory = runbuilder.write_run(tmp_path / "run", quote={"expires_at": None})
    view = build_view(load_state_directory(directory))
    assert any("records no expiry" in n.text for n in view.needs)


def test_a_corrupt_checkpoint_is_reported_not_repaired(tmp_path):
    directory = runbuilder.write_run(tmp_path / "run")
    stored = json.loads((directory / "run-plan.json").read_text())
    stored["sha256"] = "0" * 64
    (directory / "run-plan.json").write_text(json.dumps(stored))
    view = build_view(load_state_directory(directory))
    assert any(f.label == "Checkpoint integrity" and f.value == "mismatch"
               for f in view.source_facts)
    assert any("digest does not check out" in n.text for n in view.needs)


def test_without_a_checkpoint_the_operations_are_still_shown(tmp_path):
    directory = runbuilder.write_run(tmp_path / "run")
    (directory / "run-plan.json").unlink()
    view = build_view(load_state_directory(directory))
    assert view.available and view.operations and not view.steps
    page = render_page(view)
    assert "No driver checkpoint was read" in page


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------

def test_every_fact_carries_a_provenance_and_an_origin(view):
    facts = view.all_facts()
    assert len(facts) > 40
    for fact in facts:
        assert isinstance(fact.provenance, Provenance)
        assert fact.origin


def test_a_fact_without_an_origin_cannot_be_built():
    with pytest.raises(ValueError, match="no origin"):
        Fact("Chain", "FORK", Provenance.OBSERVED, "")
    with pytest.raises(ValueError, match="must be a Provenance"):
        Fact("Chain", "FORK", "OBSERVED", "somewhere")


def test_the_page_tags_exactly_as_many_values_as_the_view_has_facts(view, page):
    tagged = re.findall(r"data-prov='([A-Z]+)'", page)
    corroborations = len(re.findall(r'data-prov="OBSERVED"', page))
    assert len(tagged) == len(view.all_facts())
    assert set(tagged) <= {p.value for p in Provenance}
    assert corroborations == len(view.unattached_corroboration) + sum(
        len(s.corroboration) for s in view.steps
    )
