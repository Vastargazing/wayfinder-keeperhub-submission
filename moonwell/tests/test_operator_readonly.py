"""`operator_resolve inspect` asks the executor what it can say, and writes nothing.

This exercises the shipped helper `_lookup_evidence` loaded from its own file,
with the real `DemoKeeperHubExecutor`, the real `ReadOnlyJournal` and the real
`LocalAuthorization`. Only KeeperHub's HTTP surface, the idempotency probe, the
credential file and the workflow locator are models — the entrypoint boundary is
the helper, not the whole CLI, and no transport is live.
"""
import asyncio
import runpy
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import keeperhub_executor
import keeperhub_executor.authorization as authorization
import keeperhub_executor.reconcile as reconcile
import moonwell_demo.wiring as wiring
from wayfinder_paths.core.utils.executor import OperationJournal, build_envelope

OPERATOR = Path(__file__).resolve().parents[1] / 'scripts' / 'operator_resolve.py'
TOKEN = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
POOL = '0xA238Dd80C259a72e81d7e4664a9801593F98d1c5'
CHAIN = 8453
ENV = build_envelope({'chainId': CHAIN, 'from': wiring.WALLET, 'to': TOKEN, 'value': 0,
                      'data': '0x095ea7b3' + POOL[2:].lower().rjust(64, '0') + format(123, '064x')})


class Transport:
    """The credential is checked here, so a helper that ignored it would fail."""

    latest = None

    def __init__(self, base_url, key):
        assert key == 'SYNTHETIC-NOT-A-KEY'
        self.listings = self.sends = 0
        self.closed = False
        Transport.latest = self

    async def executions(self, workflow_id):
        self.listings += 1
        return []

    async def execute(self, *args, **kwargs):
        self.sends += 1
        raise AssertionError('inspect must never send')

    async def close(self):
        self.closed = True


class Probe:
    async def record(self, **kwargs):
        return None


def rows(database):
    connection = sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True)
    try:
        connection.row_factory = sqlite3.Row
        return [{k: row[k] for k in ('operation_id', 'digest', 'state', 'consumed', 'txn_hash')}
                for row in connection.execute('SELECT * FROM operations ORDER BY created_at')]
    finally:
        connection.close()


def schema(database):
    connection = sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True)
    try:
        return sorted(map(str, connection.execute(
            'SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()))
    finally:
        connection.close()


@pytest.fixture
def helper(tmp_path):
    """The real script, loaded without running its `asyncio.run(main())`."""
    state = tmp_path / 'state'
    state.mkdir()
    database = state / 'sdk-journal.sqlite'
    writer = OperationJournal(database)
    try:
        operation = writer.begin(ENV)
    finally:
        writer.close()

    credential = tmp_path / 'synthetic-credential.txt'
    credential.write_text('SYNTHETIC-NOT-A-KEY')

    with patch('asyncio.run', lambda coro: coro.close()):
        module = runpy.run_path(str(OPERATOR))
    module['_lookup_evidence'].__globals__['workflow_id'] = lambda: 'synthetic-workflow'

    Transport.latest = None
    with patch.object(keeperhub_executor, 'KeeperHubClient', Transport), \
            patch.object(reconcile, 'KeeperHubDbProbe', Probe), \
            patch.object(wiring, 'API_KEY_FILE', credential):
        yield dict(module=module, state=state, database=database, operation=operation,
                   before_rows=rows(database), before_schema=schema(database))


@pytest.mark.asyncio
async def test_inspect_gets_an_answer_from_the_executor_and_changes_nothing(helper):
    """Without the lifecycle hook this helper could not reach the listing at all."""
    answer = await helper['module']['_lookup_evidence'](helper['state'], helper['operation'])

    assert answer['outcome'] == 'indeterminate'
    assert 'do not resend' in (answer['detail'] or '').lower()
    assert Transport.latest is not None and Transport.latest.listings == 1
    assert Transport.latest.sends == 0
    assert Transport.latest.closed is True, 'the helper must close the transport it opened'

    assert rows(helper['database']) == helper['before_rows']
    assert schema(helper['database']) == helper['before_schema']


@pytest.mark.asyncio
async def test_inspect_does_not_answer_for_an_operation_the_journal_does_not_hold(helper):
    """An unknown id still gets an honest uncertain answer, and writes nothing."""
    answer = await helper['module']['_lookup_evidence'](helper['state'], 'not-in-this-journal')

    assert answer['outcome'] == 'indeterminate'
    assert rows(helper['database']) == helper['before_rows']
    assert Transport.latest.sends == 0


@pytest.mark.asyncio
async def test_the_helper_binds_the_diagnostic_view_not_a_writer(helper):
    """The adapter it binds must be the read-only one, whatever else it can do."""
    seen = {}
    original = authorization.ReadOnlyJournal

    class Observed(original):
        def __init__(self, path):
            super().__init__(path)
            seen['bound'] = type(self).__mro__[1].__name__

    with patch.object(authorization, 'ReadOnlyJournal', Observed):
        answer = await helper['module']['_lookup_evidence'](helper['state'], helper['operation'])

    assert seen['bound'] == 'ReadOnlyJournal'
    assert not issubclass(original, OperationJournal)
    assert answer['outcome'] == 'indeterminate'
    assert rows(helper['database']) == helper['before_rows']
