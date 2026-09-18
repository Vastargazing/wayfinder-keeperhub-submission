"""`run_loop.py status` reads an existing run and creates nothing.

Every case drives the real script: it is loaded with `runpy`, its own argparse
parses a real argv and its own `main()` runs. Only the chain client, the workflow
id file and the evidence directory are modelled. Run state is built by the real
writers — `RunPlan`, the SDK `OperationJournal` and the driver identity binding
`moonwell_demo/durable.py` writes — before any status runs.

Two observations are made directly rather than inferred from file hashes, because
an unchanged file does not prove a writer was never constructed: the writer
constructors are watched (and refused) while status runs, and SQLite is watched
through an authorizer. Each observer has a positive control in its own test.
"""
import asyncio
import contextlib
import hashlib
import io
import json
import os
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from moonwell_demo.plan import RunPlan
from moonwell_demo.wiring import M_WETH, WALLET
from wayfinder_paths.core.utils.executor import OperationJournal, build_envelope

ROOT = Path(__file__).resolve().parents[2]
RUN_LOOP = ROOT / 'moonwell/scripts/run_loop.py'
SOURCES = ('run-plan.json', 'run-plan.lock', 'sdk-journal.sqlite')
SIDECARS = ('sdk-journal.sqlite-wal', 'sdk-journal.sqlite-shm')


class FakeChain:
    """The only outside boundary status needs. It never leaves this process."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    async def fork_block(self):
        self.calls.append('fork_block')
        return 123

    async def block_number(self):
        return 123

    async def rpc(self, method, params):
        self.calls.append(f'rpc:{method}')
        assert method == 'eth_chainId'
        return '0x2105'

    async def position(self, *args, **kwargs):
        self.calls.append('position')
        return {'synthetic': True, 'block': '0x7b'}

    async def onchain_calldata_matches(self, *args, **kwargs):
        self.calls.append('onchain')
        return {'synthetic': True, 'match': True}


@pytest.fixture(scope='module')
def script():
    """The real module, loaded once; its trailing asyncio.run(main()) is neutralised."""
    with patch('asyncio.run', lambda coro: coro.close()):
        module = runpy.run_path(str(RUN_LOOP))
    return module


@pytest.fixture
def command(script, tmp_path, monkeypatch):
    """Invoke the real command and return (exit code, stdout, stderr)."""
    globals_ = script['main'].__globals__
    evidence = tmp_path / 'evidence'
    workflow = tmp_path / 'workflow-id.txt'
    workflow.write_text('synthetic-status-workflow\n')
    monkeypatch.setitem(globals_, 'Chain', FakeChain)
    monkeypatch.setitem(globals_, 'EV', evidence)
    monkeypatch.setitem(globals_, 'WF_FILE', workflow)
    monkeypatch.setitem(globals_, 'STATE', tmp_path / 'default-state')

    def run(*argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with patch.object(sys, 'argv', ['run_loop.py', *map(str, argv)]):
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    asyncio.run(script['main']())
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except BaseException as exc:      # noqa: BLE001 - reported, not swallowed
                # An escaping exception is a result to assert about, not a crash
                # that hides which rule the command broke.
                code = 1
                err.write(f'{type(exc).__name__}: {exc}')
        return code, out.getvalue(), err.getvalue()

    run.evidence = evidence
    run.globals = globals_
    return run


def snapshot(directory):
    directory = Path(directory)
    if not directory.exists():
        return {}
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob('*')) if p.is_file()}


def build_run(directory):
    """A consistent existing run, written only by the real writers."""
    directory.mkdir(parents=True, exist_ok=True)
    plan = RunPlan(directory / 'run-plan.json')
    run_id = plan.data['run_id']
    plan.record_seed(1000.0)
    plan.finish_seed()
    plan.start_iteration(10 ** 18, 2 * 10 ** 18)
    journal = OperationJournal(directory / 'sdk-journal.sqlite')
    bind_identity(journal, run_id)
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


def finish_money_call(state, run_id, txn_hash, *, record, recorded_hash=None):
    """Finish the run's monetary call, optionally recording the SDK's hash for it.

    Both halves are written by the real writers: the journal through the SDK's
    own `record_hash`, the checkpoint through `RunPlan`'s own flush.
    """
    if record:
        journal = OperationJournal(state / 'sdk-journal.sqlite')
        operation = journal.entries()[0]['operation_id']
        journal.record_hash(operation, recorded_hash or txn_hash, consumed=True)
        journal.close()
    plan = RunPlan(state / 'run-plan.json')
    try:
        plan.data['calls'][f'{run_id}/iteration/1/borrow'] = {
            'intent': {'borrow_amt_wei': 10 ** 18}, 'state': 'done', 'money': True,
            'result': txn_hash}
        plan._flush()
    finally:
        plan.close()


def bind_identity(journal, run_id):
    """The statements moonwell_demo/durable.py runs on this journal when a run starts."""
    journal._conn.execute('CREATE TABLE moonwell_run (singleton INTEGER PRIMARY KEY '
                          'CHECK(singleton=1), run_id TEXT NOT NULL)')
    journal._conn.execute('INSERT INTO moonwell_run VALUES (1,?)', (run_id,))


# Shapes that are wrong in the data itself. Each one keeps a correct digest, so
# what is being tested is the structure and not the checksum; `json` and
# `checksum` are the two cases where the file itself is the problem.
SHAPES = {
    'version': lambda data: data.__setitem__('version', 1),
    'iterations-not-a-list': lambda data: data.__setitem__('iterations', 'not a list'),
    'iteration-is-null': lambda data: data.__setitem__('iterations', [None]),
    'call-is-null': lambda data: data.__setitem__('calls', {data['run_id'] + '/x': None}),
    'call-key-is-not-a-string': lambda data: data.__setitem__('calls', {'1': {}}),
    'seed-is-not-a-mapping': lambda data: data.__setitem__('seed', 'done'),
    # The enum fields. JSON can put a list or an object where a name belongs,
    # and neither can be hashed, so a validator that asks about membership
    # before it asks about the type answers with a TypeError instead of a
    # refusal. Each of the three fields is damaged on its own, in both shapes.
    'seed-state-is-a-list': lambda data: data.__setitem__(
        'seed', {'state': [], 'usdc_amount': 1000.0}),
    'seed-state-is-a-mapping': lambda data: data.__setitem__(
        'seed', {'state': {}, 'usdc_amount': 1000.0}),
    'iteration-status-is-a-list': lambda data: data.__setitem__(
        'iterations', [{'index': 1, 'borrow_amt_wei': 10 ** 18, 'status': []}]),
    'iteration-status-is-a-mapping': lambda data: data.__setitem__(
        'iterations', [{'index': 1, 'borrow_amt_wei': 10 ** 18, 'status': {}}]),
    'call-state-is-a-list': lambda data: data.__setitem__(
        'calls', {data['run_id'] + '/x': {'intent': {}, 'state': []}}),
    'call-state-is-a-mapping': lambda data: data.__setitem__(
        'calls', {data['run_id'] + '/x': {'intent': {}, 'state': {}}}),
}

# The shapes above that are damage to the data, not to the file around it. The
# pure validator is asked about exactly these; `json` and `checksum` never reach
# it, because the file stops being readable before its data are validated.
DATA_SHAPES = tuple(SHAPES)


def corrupt(path, how):
    """Damage a checkpoint. Data shapes keep a correct digest; file damage does not."""
    stored = json.loads(path.read_text())
    if how == 'json':
        path.write_text('{ this is not json')
        return
    if how == 'checksum':
        stored['sha256'] = '0' * 64
        path.write_text(json.dumps(stored))
        return
    SHAPES[how](stored['data'])
    stored['sha256'] = hashlib.sha256(
        json.dumps(stored['data'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    path.write_text(json.dumps(stored))


# --- the matrix: what may be read, and what is refused ----------------------

def test_a_missing_run_is_refused_and_not_created(command, tmp_path):
    state = tmp_path / 'absent'
    code, out, err = command('status', '--state-dir', state, '--out', tmp_path / 'report.json')
    assert code == 2, out
    assert not state.exists(), 'a status of a run that is not there created one'
    assert 'unavailable' in err.lower()
    assert 'run state directory' in err


def test_an_empty_directory_is_refused_and_left_empty(command, tmp_path):
    state = tmp_path / 'empty'
    state.mkdir()
    code, _, err = command('status', '--state-dir', state, '--out', tmp_path / 'report.json')
    assert code == 2 and 'checkpoint' in err
    assert list(state.iterdir()) == []


def test_a_journal_without_a_checkpoint_is_refused_without_writing_one(command, tmp_path):
    state = tmp_path / 'journal-only'
    state.mkdir()
    journal = OperationJournal(state / 'sdk-journal.sqlite')
    bind_identity(journal, 'a' * 32)
    journal.close()
    before = snapshot(state)
    code, _, err = command('status', '--state-dir', state, '--out', tmp_path / 'report.json')
    assert code == 2 and 'checkpoint' in err
    assert not (state / 'run-plan.json').exists()
    assert not (state / 'run-plan.lock').exists()
    assert snapshot(state) == before


def test_a_checkpoint_without_a_journal_is_refused_without_creating_one(command, tmp_path):
    state = tmp_path / 'checkpoint-only'
    state.mkdir()
    plan = RunPlan(state / 'run-plan.json')
    plan.close()
    before = snapshot(state)
    code, _, err = command('status', '--state-dir', state, '--out', tmp_path / 'report.json')
    assert code == 2 and 'journal' in err
    assert not (state / 'sdk-journal.sqlite').exists()
    assert snapshot(state) == before


@pytest.mark.parametrize('how', ['json', 'checksum', *SHAPES])
def test_a_damaged_checkpoint_is_reported_not_repaired(command, tmp_path, how):
    """A refusal, with an exit code — not a traceback out of the reader.

    The shape cases carry a correct digest on purpose: a refusal there is about
    the structure, which a checksum mismatch would otherwise explain instead.
    """
    state = tmp_path / f'damaged-{how}'
    build_run(state)
    corrupt(state / 'run-plan.json', how)
    before = snapshot(state)
    code, _, err = command('status', '--state-dir', state, '--out', tmp_path / 'report.json')
    assert code == 2, err
    assert 'unavailable' in err.lower()
    assert 'Traceback' not in err and 'AttributeError' not in err
    assert snapshot(state) == before                 # no repair, no migration, no rewrite


def test_a_sqlite_file_that_is_not_a_journal_is_reported_not_migrated(command, tmp_path):
    state = tmp_path / 'foreign-schema'
    build_run(state)
    (state / 'sdk-journal.sqlite').unlink()
    for name in SIDECARS:
        (state / name).unlink(missing_ok=True)
    conn = sqlite3.connect(state / 'sdk-journal.sqlite')
    conn.execute('CREATE TABLE something_else (id INTEGER PRIMARY KEY)')
    conn.commit()
    conn.close()
    before = snapshot(state)
    code, _, err = command('status', '--state-dir', state, '--out', tmp_path / 'report.json')
    assert code == 2 and 'operations' in err
    assert snapshot(state)['sdk-journal.sqlite'] == before['sdk-journal.sqlite']
    tables = sqlite3.connect(state / 'sdk-journal.sqlite').execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert tables == [('something_else',)]


def test_a_journal_without_the_driver_identity_is_refused(command, tmp_path):
    state = tmp_path / 'no-identity'
    plan = RunPlan(state / 'run-plan.json')
    plan.close()
    OperationJournal(state / 'sdk-journal.sqlite').close()
    before = snapshot(state)
    code, _, err = command('status', '--state-dir', state, '--out', tmp_path / 'report.json')
    assert code == 2 and 'identity' in err
    assert snapshot(state)['run-plan.json'] == before['run-plan.json']


def test_a_journal_naming_another_run_is_refused(command, tmp_path):
    state = tmp_path / 'foreign-run'
    build_run(state)
    conn = sqlite3.connect(state / 'sdk-journal.sqlite')
    conn.execute('UPDATE moonwell_run SET run_id=?', ('b' * 32,))
    conn.commit()
    conn.close()
    code, _, err = command('status', '--state-dir', state, '--out', tmp_path / 'report.json')
    assert code == 2 and 'two different runs' in err


def test_an_existing_run_is_described_without_changing_its_sources(command, tmp_path):
    state = tmp_path / 'good'
    run_id = build_run(state)
    report = tmp_path / 'report.json'
    before = snapshot(state)
    code, out, err = command('status', '--state-dir', state, '--out', report)
    assert code == 0, err
    after = snapshot(state)
    assert {name: after[name] for name in SOURCES} == {name: before[name] for name in SOURCES}
    payload = json.loads(report.read_text())
    assert payload['run_id'] == run_id
    assert json.loads(out)['rows'] == 1
    # SQLite's read-only mode does not promise an unchanged directory; the
    # sidecars are recorded rather than asserted away.
    assert set(after) - set(before) <= set(SIDECARS)


def test_describing_a_run_twice_changes_nothing_and_says_the_same(command, tmp_path):
    state = tmp_path / 'twice'
    build_run(state)
    first = tmp_path / 'first.json'
    second = tmp_path / 'second.json'
    assert command('status', '--state-dir', state, '--out', first)[0] == 0
    before = snapshot(state)
    assert command('status', '--state-dir', state, '--out', second)[0] == 0
    assert {k: v for k, v in snapshot(state).items() if k in SOURCES} == \
           {k: v for k, v in before.items() if k in SOURCES}
    one, two = json.loads(first.read_text()), json.loads(second.read_text())
    assert one['run_id'] == two['run_id'] and one['plan'] == two['plan']


def test_status_answers_while_the_run_s_own_writer_holds_the_lock(command, tmp_path):
    """The writer keeps its lock throughout; status neither waits for it nor takes it."""
    state = tmp_path / 'locked'
    run_id = build_run(state)
    holder = subprocess.Popen(
        [sys.executable, '-c',
         'import sys, time\n'
         'from moonwell_demo.plan import RunPlan\n'
         'plan = RunPlan(sys.argv[1])\n'
         'print(plan.data["run_id"], flush=True)\n'
         'time.sleep(30)\n', str(state / 'run-plan.json')],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == run_id
        refused = subprocess.run(
            [sys.executable, '-c',
             'import sys\nfrom moonwell_demo.plan import RunPlan\nRunPlan(sys.argv[1])',
             str(state / 'run-plan.json')], capture_output=True, text=True)
        assert 'run already has a writer' in refused.stderr      # the lock really is held
        code, out, err = command('status', '--state-dir', state, '--out', tmp_path / 'r.json')
        assert code == 0, err
        assert json.loads(tmp_path.joinpath('r.json').read_text())['run_id'] == run_id
        assert holder.poll() is None                             # the writer was not disturbed
    finally:
        holder.kill()
        holder.wait()


def test_no_writer_constructor_runs_while_status_runs(command, tmp_path, monkeypatch):
    """Observed at the constructors, not inferred from file hashes."""
    state = tmp_path / 'observed'
    build_run(state)
    calls = []

    class RefusedRunPlan:
        def __init__(self, *args, **kwargs):
            calls.append('RunPlan')
            raise AssertionError('status must not construct RunPlan')

    class RefusedJournal:
        def __init__(self, *args, **kwargs):
            calls.append('OperationJournal')
            raise AssertionError('status must not construct OperationJournal')

    import wayfinder_paths.core.utils.executor as sdk
    monkeypatch.setitem(command.globals, 'RunPlan', RefusedRunPlan)
    monkeypatch.setattr('moonwell_demo.plan.RunPlan', RefusedRunPlan)
    monkeypatch.setattr(sdk, 'OperationJournal', RefusedJournal)

    code, _, err = command('status', '--state-dir', state, '--out', tmp_path / 'report.json')
    assert code == 0, err
    assert calls == []
    # The same observer, now shown to fire: a watcher that never fires proves nothing.
    with pytest.raises(AssertionError):
        sdk.OperationJournal(state / 'sdk-journal.sqlite')
    assert calls == ['OperationJournal']


def test_the_journal_is_only_read_and_the_observer_can_see_a_write(command, tmp_path):
    """SQLite authorizer: SELECT/READ only on the status path, with a writer control."""
    state = tmp_path / 'authorized'
    build_run(state)
    events = []
    real_connect = sqlite3.connect

    def watched(*args, **kwargs):
        conn = real_connect(*args, **kwargs)

        def authorizer(action, first, second, database, trigger):
            events.append(action)
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        return conn

    writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE,
              sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_CREATE_INDEX}
    with patch('sqlite3.connect', watched):
        code, _, err = command('status', '--state-dir', state, '--out', tmp_path / 'r.json')
    assert code == 0, err
    assert events, 'the authorizer saw nothing at all; it is not watching the read'
    assert not (set(events) & writes), sorted(set(events) & writes)

    control = []
    with patch('sqlite3.connect', watched):
        before = len(events)
        writer = OperationJournal(tmp_path / 'control.sqlite')
        writer.begin(build_envelope({'chainId': 8453, 'from': WALLET, 'to': M_WETH,
                                     'data': '0x', 'value': 0}))
        writer.close()
        control = events[before:]
    assert set(control) & writes, 'the observer cannot distinguish a write; the check above is empty'


def test_the_report_never_lands_inside_the_state_directory(command, tmp_path):
    state = tmp_path / 'protected'
    build_run(state)
    before = snapshot(state)
    for candidate in [state / 'status.json', state / 'run-plan.json',
                      state / 'sdk-journal.sqlite', state / 'nested/status.json']:
        code, _, err = command('status', '--state-dir', state, '--out', candidate)
        assert code == 2, (candidate, err)
        assert 'inside the run state directory' in err
    assert snapshot(state) == before
    assert not (state / 'nested').exists()


def test_the_report_never_lands_on_a_source_reached_by_another_name(command, tmp_path):
    """Another name for a source file is still that file, and is refused.

    A symlink `--out` is refused for being a symlink, before the question of what
    it points at even arises. A hardlink is checked wherever the kernel will make
    one: under Landlock ABI v1 a link whose two directories differ is refused with
    `EXDEV`, so in that isolation this alias is prepared outside the sandbox
    instead — see the delivery's own `cases.py`, which exercises it.
    """
    state = tmp_path / 'aliased'
    build_run(state)
    before = snapshot(state)
    link = tmp_path / 'link-to-checkpoint.json'
    link.symlink_to(state / 'run-plan.json')
    code, _, err = command('status', '--state-dir', state, '--out', link)
    assert code == 2, err
    assert 'is a symlink' in err
    assert link.is_symlink() and link.resolve() == (state / 'run-plan.json').resolve()

    hard = tmp_path / 'hardlink-to-checkpoint.json'
    try:
        os.link(state / 'run-plan.json', hard)
    except OSError:
        hard = None
    if hard is not None:
        code, _, err = command('status', '--state-dir', state, '--out', hard)
        assert code == 2, err
        assert 'same file as' in err
        assert hard.stat().st_ino == (state / 'run-plan.json').stat().st_ino
    assert snapshot(state) == before


def test_a_symlink_output_is_refused_even_when_it_points_somewhere_harmless(command, tmp_path):
    """The refusal is about the form of the path, not about what it happens to hit.

    A report is read back from where it was written, and its saved `stateDir` is
    relative to that directory. A name standing for a file elsewhere would make
    the two disagree, so it is refused instead of quietly followed.
    """
    state = tmp_path / 'run'
    build_run(state)
    real = tmp_path / 'reports/nested/real.json'
    real.parent.mkdir(parents=True)
    real.write_text('{}')
    alias = tmp_path / 'aliases/report.json'
    alias.parent.mkdir()
    alias.symlink_to(real)
    code, _, err = command('status', '--state-dir', state, '--out', alias)
    assert code == 2, err
    assert 'is a symlink' in err
    assert real.read_text() == '{}' and alias.is_symlink()


def test_an_output_path_with_dotdot_does_not_create_the_state_directory(command, tmp_path):
    """The path that is checked is the path that is written, normalised once.

    `<state>/../reports/x.json` names a file outside the state directory, but
    walking it literally would make the state directory on the way there — for a
    run that is being refused precisely because it does not exist.
    """
    state = tmp_path / 'missing'
    code, _, err = command('status', '--state-dir', state,
                           '--out', tmp_path / 'missing/../reports/refused.json')
    assert code == 2, err
    assert not state.exists(), 'the refusal created the run state directory on the way out'
    assert (tmp_path / 'reports/refused.json').is_file()
    assert 'run_id' not in json.loads((tmp_path / 'reports/refused.json').read_text())


def test_a_dotdot_output_writes_the_report_where_the_check_allowed_it(command, tmp_path):
    """The same normalisation on the successful path: one path, checked and used."""
    state = tmp_path / 'good/run'
    run_id = build_run(state)
    code, _, err = command('status', '--state-dir', state,
                           '--out', tmp_path / 'good/run/../../reports/status.json')
    assert code == 0, err
    report = tmp_path / 'reports/status.json'
    assert report.is_file() and json.loads(report.read_text())['run_id'] == run_id
    assert sorted(p.name for p in (tmp_path / 'good').iterdir()) == ['run']
    assert (report.parent / json.loads(report.read_text())['stateDir']).resolve() \
        == state.resolve()


def test_a_refusal_is_not_a_manifest(command, tmp_path):
    """A refusal may be written down, but never as something the console can bind."""
    state = tmp_path / 'absent'
    report = tmp_path / 'refusal.json'
    code, _, _ = command('status', '--state-dir', state, '--out', report)
    assert code == 2
    payload = json.loads(report.read_text())
    assert payload['status'] == 'unavailable' and 'run_id' not in payload
    assert payload['reason']


def test_the_default_output_stays_out_of_the_state_directory(command, tmp_path):
    state = tmp_path / 'default-out'
    build_run(state)
    before = snapshot(state)
    code, _, err = command('status', '--state-dir', state)
    assert code == 0, err
    written = sorted(p.name for p in command.evidence.rglob('*') if p.is_file())
    assert written == [f'status-{state.name}.json']
    after = snapshot(state)
    assert {k: after[k] for k in SOURCES} == {k: before[k] for k in SOURCES}
    assert set(after) - set(before) <= set(SIDECARS)


# --- the extracted checkpoint reader, against the writer it was taken from ---

@pytest.mark.parametrize('how', ['json', 'checksum', *SHAPES])
def test_the_reader_and_the_writer_agree_on_what_a_checkpoint_is(tmp_path, how):
    """One rule for both, and two distinct refusals.

    The reader raises its own ordinary `CheckpointUnreadable` and takes no lock;
    the writer keeps raising `CheckpointError`, the safety stop that must not be
    swallowed, and does take the lock. Neither is relaxed to match the other.
    """
    from moonwell_demo.plan import CheckpointError, load_checkpoint

    good = tmp_path / 'good.json'
    plan = RunPlan(good)
    run_id = plan.data['run_id']
    plan.close()
    data, digest = load_checkpoint(good)
    assert data['run_id'] == run_id
    assert digest == hashlib.sha256(good.read_bytes()).hexdigest()

    damaged = tmp_path / f'{how}.json'
    damaged.write_bytes(good.read_bytes())
    corrupt(damaged, how)
    with pytest.raises(Exception) as reader:         # noqa: PT011 - type asserted below
        load_checkpoint(damaged)
    assert type(reader.value).__name__ == 'CheckpointUnreadable'
    assert not isinstance(reader.value, CheckpointError), \
        'a reader refusal must not impersonate the writer safety stop'
    lock = damaged.with_suffix('.lock')
    assert not lock.exists(), 'the reader must not open the writer lock'
    with pytest.raises(CheckpointError, match='invalid or legacy'):
        RunPlan(damaged)
    assert lock.is_file(), 'the writer does take the lock; that is the difference'


@pytest.mark.parametrize('how', DATA_SHAPES)
def test_the_validator_refuses_every_damaged_shape_with_a_value_error(tmp_path, how):
    """The pure rule, on its own: one exception type, whatever the damage is.

    `load_checkpoint` turns exactly `ValueError` into its own refusal, and the
    writer's `__init__` reports it as the safety stop. Both of those readings
    depend on this: a validator that lets another exception type out is not
    refusing a checkpoint, it is failing to answer about one. Asserting it here,
    with no file and no lock in the picture, keeps that rule where it is stated
    instead of only where it is consumed.
    """
    from moonwell_demo.plan import validate_checkpoint

    good = tmp_path / 'good.json'
    plan = RunPlan(good)
    plan.close()
    data = json.loads(good.read_text())['data']
    validate_checkpoint(data)                        # the positive control, first

    SHAPES[how](data)
    with pytest.raises(ValueError) as refusal:
        validate_checkpoint(data)
    assert type(refusal.value) is ValueError, \
        f'the validator answered with {type(refusal.value).__name__}, not a refusal'


def test_a_valid_enum_field_is_still_accepted(tmp_path):
    """The guard rejects the wrong type; it must not reject the right value.

    Every name the driver itself writes is checked here, so a narrower type test
    than the one the writer relies on would show up as a refusal of the writer's
    own output.
    """
    from moonwell_demo.plan import validate_checkpoint

    good = tmp_path / 'good.json'
    plan = RunPlan(good)
    run_id = plan.data['run_id']
    plan.close()
    data = json.loads(good.read_text())['data']
    for seed_state in ('intent', 'done', 'external'):
        data['seed'] = {'state': seed_state, 'usdc_amount': 1000.0}
        validate_checkpoint(data)
    for status in ('in_flight', 'stopped'):
        data['iterations'] = [{'index': 1, 'borrow_amt_wei': 10 ** 18, 'status': status}]
        validate_checkpoint(data)
    data['iterations'] = [{'index': 1, 'borrow_amt_wei': 10 ** 18, 'status': 'done',
                           'lend_amt_wei': 2 * 10 ** 18}]
    validate_checkpoint(data)
    for state in ('started', 'done'):
        data['calls'] = {f'{run_id}/x': {'intent': {}, 'state': state, 'result': None}}
        validate_checkpoint(data)


def test_the_reader_refuses_a_missing_checkpoint_without_making_one(tmp_path):
    from moonwell_demo.plan import load_checkpoint

    missing = tmp_path / 'nothing.json'
    with pytest.raises(Exception) as absent:         # noqa: PT011 - type asserted below
        load_checkpoint(missing)
    assert type(absent.value).__name__ == 'CheckpointUnreadable'
    assert not missing.exists() and not missing.with_suffix('.lock').exists()


# --- the journal and the checkpoint have to describe the same run ------------

def test_an_operation_without_a_step_binding_is_refused(command, tmp_path):
    """A matching run id is not agreement: the bindings have to be there too."""
    state = tmp_path / 'unbound'
    build_run(state)
    connection = sqlite3.connect(state / 'sdk-journal.sqlite')
    connection.execute('DELETE FROM execution_steps')
    connection.commit()
    connection.close()
    report = tmp_path / 'report.json'
    code, _, err = command('status', '--state-dir', state, '--out', report)
    assert code == 2, err
    assert 'no driver step binding' in err
    assert 'run_id' not in json.loads(report.read_text())


def test_a_binding_without_its_operation_is_refused(command, tmp_path):
    state = tmp_path / 'dangling'
    build_run(state)
    connection = sqlite3.connect(state / 'sdk-journal.sqlite')
    connection.execute('DELETE FROM operations')
    connection.commit()
    connection.close()
    report = tmp_path / 'report.json'
    code, _, err = command('status', '--state-dir', state, '--out', report)
    assert code == 2, err
    assert 'which the journal does not hold' in err
    assert 'run_id' not in json.loads(report.read_text())


def test_a_finished_money_call_without_its_submitted_operation_is_refused(command, tmp_path):
    state = tmp_path / 'unproven'
    run_id = build_run(state)
    finish_money_call(state, run_id, '0x' + 'ab' * 32, record=False)
    report = tmp_path / 'report.json'
    code, _, err = command('status', '--state-dir', state, '--out', report)
    assert code == 2, err
    assert 'no submitted journal operation' in err
    assert 'run_id' not in json.loads(report.read_text())


def test_a_finished_money_call_whose_hash_disagrees_is_refused(command, tmp_path):
    state = tmp_path / 'disagreeing'
    run_id = build_run(state)
    finish_money_call(state, run_id, '0x' + 'ab' * 32, record=True,
                      recorded_hash='0x' + 'cd' * 32)
    code, _, err = command('status', '--state-dir', state, '--out', tmp_path / 'report.json')
    assert code == 2, err
    assert 'does not confirm' in err


def test_a_finished_money_call_with_its_hash_is_described(command, tmp_path):
    """The compatibility check must not refuse a run that really is consistent."""
    state = tmp_path / 'proven'
    run_id = build_run(state)
    txn_hash = '0x' + 'ab' * 32
    finish_money_call(state, run_id, txn_hash, record=True)
    report = tmp_path / 'report.json'
    code, _, err = command('status', '--state-dir', state, '--out', report)
    assert code == 0, err
    payload = json.loads(report.read_text())
    assert payload['run_id'] == run_id
    assert payload['journal'][0]['txn_hash'] == txn_hash


# --- the checkpoint must not move between being parsed and being reported ----

def test_a_checkpoint_that_changes_after_it_is_parsed_is_refused(command, tmp_path,
                                                                 monkeypatch):
    """A deterministic write boundary, not a sleep and not a claim of atomicity.

    The real reader runs; the file is replaced with another valid version of the
    same run before the reader returns. The command must refuse rather than
    publish one version's data under another version's digest.
    """
    import moonwell_demo.status as status_module

    state = tmp_path / 'moving'
    build_run(state)
    checkpoint = state / 'run-plan.json'
    original_bytes = checkpoint.read_bytes()
    stored = json.loads(original_bytes)
    old_data = json.loads(original_bytes)['data']
    stored['data']['seed']['usdc_amount'] = 2345
    stored['sha256'] = hashlib.sha256(
        json.dumps(stored['data'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    replacement = json.dumps(stored)
    real_loader = status_module.load_checkpoint

    def replace_after_parse(path):
        outcome = real_loader(path)
        if Path(path) == checkpoint:
            temporary = checkpoint.with_suffix('.moving')
            temporary.write_text(replacement)
            os.replace(temporary, checkpoint)
        return outcome

    monkeypatch.setattr(status_module, 'load_checkpoint', replace_after_parse)
    report = tmp_path / 'report.json'
    code, _, err = command('status', '--state-dir', state, '--out', report)
    assert code == 2, err
    assert 'changed while the journal was being read' in err
    payload = json.loads(report.read_text())
    assert 'run_id' not in payload and payload['status'] == 'unavailable'
    assert 'plan' not in payload, 'a refusal must not publish either version of the run'
    assert json.loads(checkpoint.read_text())['data'] != old_data   # the change did happen


def test_the_reported_digest_identifies_the_reported_checkpoint(command, tmp_path):
    state = tmp_path / 'stable'
    build_run(state)
    report = tmp_path / 'report.json'
    assert command('status', '--state-dir', state, '--out', report)[0] == 0
    payload = json.loads(report.read_text())
    assert payload['checkpointSha256'] == hashlib.sha256(
        (state / 'run-plan.json').read_bytes()).hexdigest()
    assert payload['plan'] == json.loads((state / 'run-plan.json').read_text())['data']
