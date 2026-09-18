"""Mode labels are the run's own, or they are visibly absent.

A fork run must never look like a mainnet run, and a dev-signer run must never
look like a real-signer run. The screen has no default to fall back on: the only
place a label can come from is a manifest that names this state directory.
"""

import json

import pytest

import runbuilder
from operator_console.model import REQUIRED_MODE_KEYS, UNDECLARED, build_view, mode_facts
from operator_console.provenance import Provenance
from operator_console.render import render_page
from operator_console.sources import load_state_directory

#: Labels that would misrepresent a fork/dev-signer run as something stronger.
OVERCLAIMS = ("MAINNET", "REAL SIGNER", "TURNKEY", "TESTNET", "PRODUCTION", "LIVE")


def mode_values(view):
    return {f.label: f for f in view.mode_facts}


def test_a_fork_dev_signer_run_is_labelled_fork_and_dev_signer(view):
    facts = mode_values(view)
    assert facts["Chain"].value == runbuilder.FORK_MODE_LABELS["chain"]
    assert facts["Signer"].value == runbuilder.FORK_MODE_LABELS["signer"]
    assert facts["Chain"].value.startswith("FORK")
    assert "DEV/TEST SIGNER" in facts["Signer"].value
    assert all(f.provenance is Provenance.OBSERVED for f in view.mode_facts)


def test_a_fork_dev_signer_run_is_never_labelled_mainnet_or_real_signer(view, page):
    """No mode value may assert a stronger mode than the run recorded.

    The check is identity, not word-spotting: a run's own labels legitimately
    contain "fork of Base mainnet" and "not Turnkey", and the property that
    matters is that the screen shows the run's strings and never one of its own.
    """
    declared = set(runbuilder.FORK_MODE_LABELS.values())
    for fact in view.mode_facts:
        assert fact.value in declared or fact.value == UNDECLARED, fact

    facts = mode_values(view)
    assert facts["Chain"].value.upper().startswith("FORK")
    assert "DEV/TEST SIGNER" in facts["Signer"].value
    assert "not Turnkey" in facts["Signer"].value
    assert not facts["Signer"].value.upper().startswith("REAL")

    # The rendered mode block reproduces the declared labels verbatim, and the
    # screen's own vocabulary contributes no mode word of its own.
    for value in declared:
        assert value in page
    assert "MAINNET (" not in page.upper()
    assert "REAL SIGNER" not in page.upper()


def test_the_screen_has_no_stronger_label_to_fall_back_on():
    """Whatever the run says, the console's own defaults are only UNDECLARED."""
    for fact in mode_facts(None, ""):
        assert fact.value == UNDECLARED
        for overclaim in OVERCLAIMS:
            assert overclaim not in fact.value.upper()
            assert overclaim not in fact.note.upper()


def test_the_screen_supplies_no_label_of_its_own():
    """With no manifest there is no label at all, and nothing is defaulted."""
    facts = mode_facts(None, "")
    assert [f.label for f in facts] == ["Chain", "Signer", "KeeperHub", "SDK key material"]
    for fact in facts:
        assert fact.value == UNDECLARED
        assert fact.provenance is Provenance.UNVERIFIED
        assert "no run manifest" in fact.note
    assert len(facts) == len(REQUIRED_MODE_KEYS)


def test_an_unlabelled_run_renders_undeclared_and_nothing_stronger(tmp_path):
    directory = runbuilder.write_run(tmp_path / "run", write_manifest=False)
    view = build_view(load_state_directory(directory))
    assert all(f.value == UNDECLARED for f in view.mode_facts)
    page = render_page(view)
    assert "UNDECLARED" in page
    assert "not a mainnet mode and not a real-signer mode" in page


def test_labels_are_never_carried_from_one_run_to_another(tmp_path):
    """Another run's manifest cannot label this one, however convenient it is."""
    labelled = runbuilder.write_run(tmp_path / "run-a")
    unlabelled = runbuilder.write_run(tmp_path / "run-b", write_manifest=False)

    state = load_state_directory(unlabelled, labelled / "run-manifest.json")
    assert state.manifest is not None and state.manifest.bound is False
    assert "never carried from one run to another" in state.manifest.binding_detail

    view = build_view(state)
    assert all(f.value == UNDECLARED for f in view.mode_facts)
    assert "FORK" not in " ".join(f.value for f in view.mode_facts)
    page = render_page(view)
    assert "refused" in page
    assert runbuilder.FORK_MODE_LABELS["signer"] not in page


def test_a_manifest_with_no_state_dir_cannot_label_anything(tmp_path):
    directory = runbuilder.write_run(tmp_path / "run")
    manifest = directory / "run-manifest.json"
    data = json.loads(manifest.read_text())
    del data["stateDir"]
    manifest.write_text(json.dumps(data))
    view = build_view(load_state_directory(directory, manifest))
    assert all(f.value == UNDECLARED for f in view.mode_facts)
    assert any("names no stateDir" in f.note for f in view.mode_facts)


def test_a_declared_label_that_is_missing_is_shown_missing(tmp_path):
    directory = runbuilder.write_run(
        tmp_path / "run", mode_labels={"chain": "FORK (anvil fork of Base mainnet)"}
    )
    view = build_view(load_state_directory(directory, directory / "run-manifest.json"))
    facts = mode_values(view)
    assert facts["Chain"].value.startswith("FORK")
    for label in ("Signer", "KeeperHub", "SDK key material"):
        assert facts[label].value == UNDECLARED
        assert facts[label].provenance is Provenance.UNVERIFIED


def test_the_fixture_builder_marks_its_output_as_a_fixture(tmp_path):
    pytest.importorskip("wayfinder_paths")
    from operator_console.fixture import build_fixture

    directory = build_fixture(tmp_path / "fixture")
    assert (directory / "FIXTURE.txt").is_file()
    view = build_view(load_state_directory(directory, directory / "run-manifest.json"))
    assert view.fixture_notice
    page = render_page(view)
    assert "FIXTURE DATA — this is not a run" in page
    assert "Nothing was broadcast anywhere" in page
    for fact in view.mode_facts:
        for overclaim in OVERCLAIMS:
            assert overclaim not in fact.value.upper()
