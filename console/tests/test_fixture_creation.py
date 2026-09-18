"""``--make-fixture`` builds a new directory of its own, or it builds nothing.

Synthetic data shaped like a run must never land on top of a real one, so every
path that is already taken is refused and left exactly as it was found: same
names, same bytes, same mtimes, and no entry added — a SQLite connection, even
a read-only one, can create or touch the WAL and SHM sidecars beside a
journal. This comparison checks file preservation; an unchanged listing alone
does not prove that the journal was never opened. The builds that are allowed are read back through the console's
own reader, because a fixture that cannot be read is not a fixture.

The refusal type is checked by name rather than imported at module scope: a
builder that has no such error must fail these tests by returning a fixture it
was asked not to build, not by failing to import.
"""

import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import runbuilder
from operator_console.fixture import build_fixture
from operator_console.model import build_view
from operator_console.render import render_page
from operator_console.sources import load_state_directory

#: The error ``fixture.py`` raises for a path it will not claim.
REFUSAL = "FixtureTargetError"

CONSOLE = Path(__file__).resolve().parent.parent


def snapshot(directory):
    """Every entry under a directory: bytes and mtime, with links not followed."""
    entries = {}
    for path in sorted(directory.rglob("*")):
        name = str(path.relative_to(directory))
        if path.is_symlink():
            entries[name] = ("symlink", str(path.readlink()))
        elif path.is_dir():
            entries[name] = ("directory", None)
        else:
            entries[name] = (path.read_bytes(), path.stat().st_mtime_ns)
    return entries


def refused(target):
    """Build on a path that must be refused, and return the refusal."""
    with pytest.raises(Exception) as refusal:  # noqa: PT011 - narrowed on the next line
        build_fixture(target)
    assert type(refusal.value).__name__ == REFUSAL, refusal.value
    return refusal.value


def read_only(database, query):
    """Read a journal without writing to it, for checking a refusal held."""
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        return connection.execute(query).fetchall()
    finally:
        connection.close()


def console(*args, cwd=None):
    """Run the real CLI in its own process, on this checkout's code."""
    return subprocess.run(
        [sys.executable, "-m", "operator_console", *map(str, args)],
        capture_output=True, text=True, cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(CONSOLE)},
    )


def has_open_descriptor(path):
    """Whether this process still holds a descriptor on a path."""
    wanted = os.path.realpath(path)
    for entry in Path("/proc/self/fd").iterdir():
        try:
            if os.path.realpath(entry.readlink()) == wanted:
                return True
        except OSError:
            continue
    return False


# --- paths that are already taken -------------------------------------------

def test_a_run_directory_is_refused_and_left_exactly_as_it_was(tmp_path):
    """The case that matters: a state directory someone's run is recorded in.

    Overwriting it replaced the saved run identity and mixed fixture operations
    into the journal, which is the loss this refusal exists to prevent.
    """
    state = runbuilder.write_run(tmp_path / "run")
    (state / "operator-notes.txt").write_text("what this run was for\n")
    before = snapshot(state)

    message = str(refused(state))
    assert str(state) in message
    assert "new directory" in message

    assert snapshot(state) == before
    assert not (state / "FIXTURE.txt").exists()
    # Read the originals back only after the comparison above, since reading a
    # WAL-mode journal may create sidecars of its own.
    checkpoint = json.loads((state / "run-plan.json").read_text())
    assert checkpoint["data"]["run_id"] == runbuilder.RUN_ID
    rows = read_only(state / "sdk-journal.sqlite", "SELECT sender FROM operations")
    assert [sender for (sender,) in rows] == [runbuilder.WALLET] * 3


def test_an_existing_empty_directory_is_refused(tmp_path):
    """Empty is still taken: the builder claims a name, not free space."""
    target = tmp_path / "empty"
    target.mkdir()
    refused(target)
    assert snapshot(target) == {}


def test_an_existing_file_is_refused(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("not a state directory\n")
    refused(target)
    assert target.read_text() == "not a state directory\n"


def test_a_finished_fixture_is_refused_rather_than_rebuilt(tmp_path):
    """There is no idempotent rebuild; a second run of the demo needs a new path."""
    fixture = build_fixture(tmp_path / "fixture")
    before = snapshot(fixture)
    refused(fixture)
    assert snapshot(fixture) == before


def test_a_partial_fixture_is_refused_rather_than_completed(tmp_path):
    """What a failed build left behind is not a directory to continue into."""
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "FIXTURE.txt").write_text("half-written\n")
    before = snapshot(partial)
    refused(partial)
    assert snapshot(partial) == before


def test_a_symlink_to_a_run_directory_is_refused_and_the_target_is_untouched(tmp_path):
    state = runbuilder.write_run(tmp_path / "run")
    before = snapshot(state)
    link = tmp_path / "link"
    link.symlink_to(state)

    refused(link)

    assert link.is_symlink() and link.readlink() == state
    assert snapshot(state) == before
    assert not (state / "FIXTURE.txt").exists()


def test_a_dangling_symlink_is_refused(tmp_path):
    """Nothing is there to lose, and the link is still not a directory to claim."""
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "nowhere")
    refused(link)
    assert link.is_symlink()
    assert not (tmp_path / "nowhere").exists()


def test_a_symlinked_parent_is_refused_and_the_decoy_is_untouched(tmp_path):
    """A link standing in for a directory in the path would be followed by mkdir."""
    decoy = runbuilder.write_run(tmp_path / "decoy")
    before = snapshot(decoy)
    link = tmp_path / "parent-link"
    link.symlink_to(decoy)

    message = str(refused(link / "fixture"))
    assert "symlink" in message

    assert snapshot(decoy) == before
    assert not (decoy / "fixture").exists()


# --- paths that are free -----------------------------------------------------

def test_a_new_absolute_path_is_built_and_reads_back(tmp_path):
    directory = build_fixture(tmp_path / "fixture")

    assert (directory / "FIXTURE.txt").read_text().startswith("Synthetic data")
    manifest = json.loads((directory / "run-manifest.json").read_text())
    checkpoint = json.loads((directory / "run-plan.json").read_text())["data"]
    saved, = read_only(directory / "sdk-journal.sqlite",
                       "SELECT run_id FROM moonwell_run")[0]
    assert manifest["run_id"] == checkpoint["run_id"] == saved

    view = build_view(load_state_directory(directory, directory / "run-manifest.json"))
    assert view.fixture_notice
    page = render_page(view)
    assert "FIXTURE DATA — this is not a run" in page


def test_a_new_relative_path_is_built(tmp_path, monkeypatch):
    """A relative path is a normal way to name a new directory, and still works."""
    monkeypatch.chdir(tmp_path)
    directory = build_fixture(Path("fixture-run"))
    assert (tmp_path / "fixture-run/FIXTURE.txt").is_file()
    assert build_view(load_state_directory(directory)).fixture_notice


# --- the chain of parents ----------------------------------------------------

def test_a_path_whose_parents_do_not_exist_is_built(tmp_path):
    """Missing parents are made for a build that goes ahead."""
    directory = build_fixture(tmp_path / "one/two/three/fixture")
    assert (directory / "FIXTURE.txt").is_file()
    assert build_view(load_state_directory(directory)).fixture_notice


def test_a_path_that_walks_back_up_is_refused_before_any_parent_is_made(tmp_path):
    """`..` can create an unwanted parent on the way to an occupied target.

    The single ``mkdir`` claims the final component, not the chain above it. A
    path like ``detour/../run`` would have ``detour`` created before the kernel
    could report that ``run`` is taken, so the refusal comes first instead.
    """
    state = runbuilder.write_run(tmp_path / "run")
    before = snapshot(state)
    detour = tmp_path / "detour"

    message = str(refused(detour / ".." / "run"))
    assert ".." in message

    assert not detour.exists()
    assert snapshot(state) == before


def test_a_parent_component_that_is_a_file_is_refused_and_creates_nothing(tmp_path):
    blocker = tmp_path / "notes.txt"
    blocker.write_text("not a directory\n")
    refused(blocker / "sub/fixture")
    assert blocker.is_file() and blocker.read_text() == "not a directory\n"
    assert not (tmp_path / "notes.txt/sub").exists()


# --- two builders, one free path --------------------------------------------

def _race(target, barrier, answers):
    """Wait for the other builder, then both go for the same free path."""
    barrier.wait(timeout=30)
    try:
        build_fixture(target)
    except BaseException as refusal:  # noqa: BLE001 - the answer is the point
        answers.put(("refused", type(refusal).__name__))
    else:
        answers.put(("built", None))


def test_two_builders_racing_for_one_free_path_produce_one_fixture(tmp_path):
    """Checking the path is free and then creating it would let both through."""
    target = tmp_path / "contested"
    context = multiprocessing.get_context("fork")
    barrier, answers = context.Barrier(2), context.Queue()
    builders = [context.Process(target=_race, args=(target, barrier, answers))
                for _ in range(2)]
    for builder in builders:
        builder.start()
    outcomes = sorted(answers.get(timeout=60) for _ in builders)
    for builder in builders:
        builder.join(timeout=60)
        assert builder.exitcode == 0

    assert outcomes == [("built", None), ("refused", REFUSAL)]
    assert build_view(load_state_directory(target)).fixture_notice


# --- failing after the directory is claimed ----------------------------------

def test_a_failure_after_the_directory_is_claimed_is_not_a_success(tmp_path):
    """A half-built directory stays half-built, and says so instead of passing."""
    assert Path("/proc/self/fd").is_dir(), "this check needs /proc"
    import wayfinder_paths.core.utils.executor as sdk

    neighbour = runbuilder.write_run(tmp_path / "neighbour")
    untouched = snapshot(neighbour)
    target = tmp_path / "half-built"
    journal = target / "sdk-journal.sqlite"

    real, seen = sdk.build_envelope, []

    def fail_once_the_journal_is_open(*args, **kwargs):
        seen.append(args)
        if len(seen) == 3:
            raise RuntimeError("synthetic failure while writing the fixture")
        return real(*args, **kwargs)

    sdk.build_envelope = fail_once_the_journal_is_open
    try:
        with pytest.raises(RuntimeError) as failure:
            build_fixture(target)
    finally:
        sdk.build_envelope = real

    assert any(str(target) in note for note in getattr(failure.value, "__notes__", []))
    # The marker is written before anything that looks like a run.
    assert (target / "FIXTURE.txt").is_file()
    assert not (target / "run-plan.json").exists()
    assert not (target / "run-manifest.json").exists()
    assert journal.is_file()
    assert not has_open_descriptor(journal)
    # Both directions, so a detector that always answers "no" cannot pass here.
    probe = journal.open("rb")
    assert has_open_descriptor(journal)
    probe.close()

    remains = snapshot(target)
    refused(target)
    assert snapshot(target) == remains
    assert snapshot(neighbour) == untouched


# --- the command line --------------------------------------------------------

def test_the_cli_refuses_an_occupied_directory_without_a_traceback(tmp_path):
    state = runbuilder.write_run(tmp_path / "run")
    before = snapshot(state)

    result = console("--make-fixture", state)

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert str(state) in result.stderr
    assert "fixture state directory:" not in result.stdout
    assert snapshot(state) == before


def test_the_cli_builds_a_new_directory(tmp_path):
    """The positive control for the case above: a free path still works."""
    target = tmp_path / "fixture"
    result = console("--make-fixture", target)
    assert result.returncode == 0, result.stderr
    assert "fixture state directory:" in result.stdout
    assert (target / "FIXTURE.txt").is_file()


def test_the_cli_help_names_the_modes_that_write():
    """Reading the screen is read-only; two flags write files and say so."""
    result = console("--help")
    assert result.returncode == 0
    text = " ".join(result.stdout.split())

    assert "Serving and probing read existing state" in text
    assert "choose an output file outside the state directory" in text
    assert "create a labelled fixture state directory at a new path" in text
    assert "write the page to this file" in text
    assert "Every mode reads" not in text


def test_the_refusal_is_a_named_error_a_caller_can_catch(tmp_path):
    """A library caller gets an explicit error, not a return code to guess at."""
    from operator_console.fixture import FixtureTargetError

    assert FixtureTargetError.__name__ == REFUSAL
    target = tmp_path / "taken"
    target.mkdir()
    with pytest.raises(FixtureTargetError):
        build_fixture(target)
