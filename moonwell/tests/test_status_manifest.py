"""What `run_loop.py status` writes is a manifest the operator console can bind.

The consumer here is the real, unmodified `operator_console.sources`: these tests
import it and hand it exactly the bytes the producer wrote. Nothing is edited
after the producer, and the consumer's rules are never relaxed for it — a report
that does not name this run must keep being refused.

The producer is the real script, loaded with `runpy` and driven through its own
argparse; only the chain client, the workflow id file and the evidence directory
are modelled.
"""
import asyncio
import contextlib
import io
import json
import os
import runpy
import shutil
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from moonwell_demo.plan import RunPlan
from moonwell_demo.wiring import M_WETH, WALLET
from operator_console.sources import load_state_directory, read_manifest
from wayfinder_paths.core.utils.executor import OperationJournal, build_envelope

ROOT = Path(__file__).resolve().parents[2]
RUN_LOOP = ROOT / 'moonwell/scripts/run_loop.py'


class FakeChain:
    def __init__(self, *args, **kwargs):
        pass

    async def fork_block(self):
        return 123

    async def block_number(self):
        return 123

    async def rpc(self, method, params):
        assert method == 'eth_chainId'
        return '0x2105'

    async def position(self, *args, **kwargs):
        return {'synthetic': True, 'block': '0x7b'}

    async def onchain_calldata_matches(self, *args, **kwargs):
        return {'synthetic': True, 'match': True}


@pytest.fixture(scope='module')
def script():
    with patch('asyncio.run', lambda coro: coro.close()):
        return runpy.run_path(str(RUN_LOOP))


@pytest.fixture
def command(script, tmp_path, monkeypatch):
    globals_ = script['main'].__globals__
    workflow = tmp_path / 'workflow-id.txt'
    workflow.write_text('synthetic-status-workflow\n')
    monkeypatch.setitem(globals_, 'Chain', FakeChain)
    monkeypatch.setitem(globals_, 'EV', tmp_path / 'evidence')
    monkeypatch.setitem(globals_, 'WF_FILE', workflow)
    monkeypatch.setitem(globals_, 'STATE', tmp_path / 'default-state')

    def run(*argv, cwd=None):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        previous = Path.cwd()
        if cwd is not None:
            os.chdir(cwd)
        try:
            with patch.object(sys, 'argv', ['run_loop.py', *map(str, argv)]):
                try:
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        asyncio.run(script['main']())
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
                except BaseException as exc:   # noqa: BLE001 - reported, not swallowed
                    code = 1
                    err.write(f'{type(exc).__name__}: {exc}')
        finally:
            os.chdir(previous)
        return code, out.getvalue(), err.getvalue()

    return run


def build_run(directory):
    """A consistent existing run, written only by the real writers."""
    directory.mkdir(parents=True, exist_ok=True)
    plan = RunPlan(directory / 'run-plan.json')
    run_id = plan.data['run_id']
    plan.record_seed(1000.0)
    plan.finish_seed()
    plan.start_iteration(10 ** 18, 2 * 10 ** 18)
    journal = OperationJournal(directory / 'sdk-journal.sqlite')
    journal._conn.execute('CREATE TABLE moonwell_run (singleton INTEGER PRIMARY KEY '
                          'CHECK(singleton=1), run_id TEXT NOT NULL)')
    journal._conn.execute('INSERT INTO moonwell_run VALUES (1,?)', (run_id,))
    key = f'{run_id}/iteration/1/borrow'
    journal.bind_step(key + '/send/0', build_envelope(
        {'chainId': 8453, 'from': WALLET, 'to': M_WETH,
         'data': '0xc5ebeaec' + '00' * 31 + '0a', 'value': 0}))
    plan.data['calls'][key] = {'intent': {'borrow_amt_wei': 10 ** 18}, 'state': 'started',
                               'money': True}
    plan._flush()
    journal.close()
    plan.close()
    return run_id


def bind(state, report):
    """Exactly the bytes the producer wrote, through the real console reader."""
    return load_state_directory(state, report).manifest


# --- the producer's own report, in the places an operator would put it -------

def test_an_absolute_state_path_produces_a_bound_manifest(command, tmp_path):
    state = tmp_path / 'run'
    run_id = build_run(state)
    report = tmp_path / 'reports/status.json'
    code, _, err = command('status', '--state-dir', state, '--out', report)
    assert code == 0, err
    written = report.read_bytes()
    manifest = bind(state, report)
    assert manifest.bound, manifest.binding_detail
    assert report.read_bytes() == written, 'the report was edited after the producer wrote it'
    assert json.loads(written)['run_id'] == run_id
    assert manifest.mode_labels and manifest.not_proven


def test_a_relative_state_path_from_another_directory_still_names_this_run(command, tmp_path):
    """The saved path is computed from the real paths, not copied from argv."""
    state = tmp_path / 'run'
    build_run(state)
    # Different depths, so a path relative to the producer's directory cannot
    # also happen to be the path the reader needs.
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    report = tmp_path / 'reports/nested/status.json'
    argument = os.path.relpath(state, elsewhere)
    code, _, err = command('status', '--state-dir', argument, '--out', report, cwd=elsewhere)
    assert code == 0, err
    saved = json.loads(report.read_text())['stateDir']
    assert saved != argument, 'the literal CLI argument is not a path the reader can use'
    assert (report.parent / saved).resolve() == state.resolve()
    assert bind(state, report).bound


def test_a_report_written_far_from_its_run_still_binds(command, tmp_path):
    state = tmp_path / 'a/b/c/run'
    build_run(state)
    report = tmp_path / 'x/y/status.json'
    assert command('status', '--state-dir', state, '--out', report)[0] == 0
    assert bind(state, report).bound


def test_describing_the_same_run_again_names_the_same_run(command, tmp_path):
    state = tmp_path / 'run'
    run_id = build_run(state)
    first, second = tmp_path / 'one.json', tmp_path / 'two.json'
    assert command('status', '--state-dir', state, '--out', first)[0] == 0
    assert command('status', '--state-dir', state, '--out', second)[0] == 0
    assert bind(state, first).bound and bind(state, second).bound
    assert json.loads(first.read_text())['run_id'] == run_id
    assert json.loads(second.read_text())['run_id'] == run_id


def test_a_bundle_moved_as_a_whole_still_binds(command, tmp_path):
    """State and report keep their relative positions, so the bundle travels."""
    bundle = tmp_path / 'bundle'
    state = bundle / 'state'
    build_run(state)
    report = bundle / 'reports/status.json'
    assert command('status', '--state-dir', state, '--out', report)[0] == 0
    assert bind(state, report).bound

    moved = tmp_path / 'moved'
    shutil.move(bundle, moved)
    moved_state, moved_report = moved / 'state', moved / 'reports/status.json'
    assert json.loads(moved_report.read_text())['stateDir'] == '../state'
    assert bind(moved_state, moved_report).bound


# --- what must keep being refused -------------------------------------------

def test_a_journal_that_names_another_run_produces_no_bound_manifest(command, tmp_path):
    state = tmp_path / 'run'
    build_run(state)
    conn = sqlite3.connect(state / 'sdk-journal.sqlite')
    conn.execute('UPDATE moonwell_run SET run_id=?', ('c' * 32,))
    conn.commit()
    conn.close()
    report = tmp_path / 'status.json'
    code, _, err = command('status', '--state-dir', state, '--out', report)
    assert code == 2, err
    payload = json.loads(report.read_text())
    assert 'run_id' not in payload and payload['status'] == 'unavailable'
    assert not bind(state, report).bound


def test_a_run_without_a_saved_identity_produces_no_bound_manifest(command, tmp_path):
    state = tmp_path / 'run'
    plan = RunPlan(state / 'run-plan.json')
    plan.close()
    OperationJournal(state / 'sdk-journal.sqlite').close()
    report = tmp_path / 'status.json'
    assert command('status', '--state-dir', state, '--out', report)[0] == 2
    assert 'run_id' not in json.loads(report.read_text())
    assert not bind(state, report).bound


def test_one_run_s_report_is_refused_for_another_run(command, tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    build_run(first)
    build_run(second)
    report = tmp_path / 'first-status.json'
    assert command('status', '--state-dir', first, '--out', report)[0] == 0
    assert bind(first, report).bound
    other = bind(second, report)
    assert not other.bound
    assert 'not' in other.binding_detail


def test_a_foreign_run_id_in_an_otherwise_valid_report_is_refused(command, tmp_path):
    """The consumer is not relaxed: only the run's own identity binds."""
    state = tmp_path / 'run'
    build_run(state)
    report = tmp_path / 'status.json'
    assert command('status', '--state-dir', state, '--out', report)[0] == 0
    payload = json.loads(report.read_text())
    payload['run_id'] = 'd' * 32
    forged = tmp_path / 'forged.json'
    forged.write_text(json.dumps(payload))
    manifest = read_manifest(forged, state,
                             journal=load_state_directory(state).journal,
                             checkpoint=load_state_directory(state).checkpoint)
    assert not manifest.bound and 'contradiction' in manifest.binding_detail


def test_a_report_in_a_symlinked_directory_still_binds(command, tmp_path):
    """A symlinked parent is fine: producer and reader resolve it the same way.

    Only a symlink as the report's own name is ambiguous, and that is refused.
    """
    state = tmp_path / 'run'
    build_run(state)
    real = tmp_path / 'real-reports'
    real.mkdir()
    link = tmp_path / 'reports'
    link.symlink_to(real, target_is_directory=True)
    report = link / 'status.json'
    code, _, err = command('status', '--state-dir', state, '--out', report)
    assert code == 0, err
    assert (real / 'status.json').is_file()
    assert bind(state, report).bound
    assert bind(state, real / 'status.json').bound


def test_a_symlink_named_as_the_report_is_refused(command, tmp_path):
    state = tmp_path / 'run'
    build_run(state)
    real = tmp_path / 'reports/real.json'
    real.parent.mkdir()
    real.write_text('{}')
    alias = tmp_path / 'alias.json'
    alias.symlink_to(real)
    code, _, err = command('status', '--state-dir', state, '--out', alias)
    assert code == 2 and 'is a symlink' in err
    assert real.read_text() == '{}'


def test_the_reader_resolves_the_saved_path_from_the_report_s_own_directory(command, tmp_path):
    """A copy of the report placed elsewhere no longer names the run, and is refused."""
    state = tmp_path / 'run'
    build_run(state)
    report = tmp_path / 'reports/status.json'
    assert command('status', '--state-dir', state, '--out', report)[0] == 0
    elsewhere = tmp_path / 'reports/deeper/status.json'
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(report.read_bytes())
    assert bind(state, report).bound
    assert not bind(state, elsewhere).bound
