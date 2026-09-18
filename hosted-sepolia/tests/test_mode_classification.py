"""The profile selector must read the mode, never infer it from absence."""

import pytest

from hosted_sepolia.verify import ExecutionMode, classify_execution_mode


def test_step_flag_true_selects_sponsored():
    f = classify_execution_mode({"transactionHash": "0xabc"}, {"sponsored": True})
    assert f.mode is ExecutionMode.SPONSORED
    assert "outputRaw.sponsored" in f.sources


def test_trace_wrapper_alone_is_unknown():
    f = classify_execution_mode({"executedCall": {"sponsored": True}})
    assert f.mode is ExecutionMode.UNKNOWN


def test_trace_direct_alone_is_unknown():
    f = classify_execution_mode({"executedCall": {"sponsored": False}})
    assert f.mode is ExecutionMode.UNKNOWN


def test_absent_sponsored_is_unknown_not_direct():
    # THE critical case. KeeperHub's direct branch never writes
    # `sponsored: false`, so absence is ambiguous between "direct" and "the
    # field was never written". Reading it as direct would then run the
    # top-level comparison against a sponsored transaction and mislabel a
    # legitimate send as a mismatch -- or worse, be relaxed to "fix" it.
    f = classify_execution_mode({"transactionHash": "0xabc", "success": True})
    assert f.mode is ExecutionMode.UNKNOWN
    assert "absence cannot be read as direct" in f.detail


def test_empty_and_none_outputs_are_unknown():
    assert classify_execution_mode(None).mode is ExecutionMode.UNKNOWN
    assert classify_execution_mode({}).mode is ExecutionMode.UNKNOWN
    assert classify_execution_mode({}, {}).mode is ExecutionMode.UNKNOWN


def test_conflicting_sources_are_unknown_not_a_tie_break():
    f = classify_execution_mode({"executedCall": {"sponsored": False}}, {"sponsored": True})
    assert f.mode is ExecutionMode.UNKNOWN
    assert "conflict" in f.detail


def test_both_agreeing_on_sponsored_names_both_sources():
    f = classify_execution_mode({"executedCall": {"sponsored": True}}, {"sponsored": True})
    assert f.mode is ExecutionMode.SPONSORED
    assert len(f.sources) == 2


def test_non_boolean_sponsored_is_ignored():
    # A truthy string must not be read as a positive report.
    f = classify_execution_mode({"sponsored": "true"})
    assert f.mode is ExecutionMode.UNKNOWN


@pytest.mark.parametrize("value", [1, 0, None, [], {}])
def test_only_real_booleans_count(value):
    assert classify_execution_mode({"sponsored": value}).mode is ExecutionMode.UNKNOWN
