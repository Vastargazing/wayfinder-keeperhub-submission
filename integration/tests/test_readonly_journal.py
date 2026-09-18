"""The diagnostic journal against the real executor's lifecycle.

`ReadOnlyJournal` is the view an operator inspection binds to a running
executor. The executor calls `invalidate_validation()` on its journal at every
attempt boundary, so a view that does not answer it cannot be inspected at all —
and, worse, replaces a failure being reported with an `AttributeError` of its
own. These tests hold the view to the lifecycle the executor actually requires,
and to reading *only*: no SQL write, no schema migration, no created file, and
no transition of the operation it is looking at.

Transport, the idempotency probe and the chain read are models; the journal,
the executor, `LocalAuthorization` and the adapter are the shipped code.
"""
import asyncio
import copy
import sqlite3

import pytest
from keeperhub_executor.authorization import ReadOnlyJournal
from keeperhub_executor.executor import KeeperHubExecutor, encode_from_recorded_input
from wayfinder_paths.core.utils.executor import (
    ExecutionOutcomeUnknownError, OperationJournal, build_envelope)

WALLET = '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266'
TOKEN = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
POOL = '0xA238Dd80C259a72e81d7e4664a9801593F98d1c5'
CHAIN = 8453
HASH = '0x' + 'ab' * 32
ENV = build_envelope({'chainId': CHAIN, 'from': WALLET, 'to': TOKEN, 'value': 0,
                      'data': '0x095ea7b3' + POOL[2:].lower().rjust(64, '0') + format(123, '064x')})

#: Authorizer actions that would mean the adapter changed something.
WRITE_ACTIONS = {getattr(sqlite3, name): name for name in (
    'SQLITE_INSERT', 'SQLITE_UPDATE', 'SQLITE_DELETE', 'SQLITE_CREATE_TABLE',
    'SQLITE_CREATE_INDEX', 'SQLITE_CREATE_TEMP_TABLE', 'SQLITE_DROP_TABLE',
    'SQLITE_DROP_INDEX', 'SQLITE_ALTER_TABLE') if hasattr(sqlite3, name)}


class Transport:
    """KeeperHub's HTTP surface. Sending is refused here, not merely unused."""

    def __init__(self):
        self.listings = self.log_reads = self.sends = 0
        self.row = None
        self.listing_error = None
        self.logs_error = None

    async def executions(self, workflow_id):
        self.listings += 1
        if self.listing_error:
            raise self.listing_error
        return [copy.deepcopy(self.row)] if self.row else []

    async def execution_logs(self, execution_id):
        self.log_reads += 1
        if self.logs_error:
            raise self.logs_error
        return {'execution': copy.deepcopy(self.row),
                'logs': [{'executionId': 'e', 'nodeId': 'action-1',
                          'nodeType': 'web3/write-contract', 'status': 'success',
                          'output': {'transactionHash': HASH, 'chainId': CHAIN}}]}

    async def execute(self, *args, **kwargs):
        self.sends += 1
        raise AssertionError('a diagnostic lookup must never reach the send boundary')

    async def close(self):
        pass


def journal_with_one_pending(tmp_path):
    """A real SDK journal holding one pending operation, written by the SDK."""
    database = tmp_path / 'sdk-journal.sqlite'
    writer = OperationJournal(database)
    try:
        return database, writer.begin(ENV)
    finally:
        writer.close()


def identity(database):
    """The fields a transition would change, read on a separate connection."""
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


def watch(connection, seen):
    """Record every SQL action the adapter's own connection is authorized for."""
    def authorize(action, arg1, arg2, database, trigger):
        seen.append((action, WRITE_ACTIONS.get(action), arg1))
        return sqlite3.SQLITE_OK
    connection.set_authorizer(authorize)


def build(transport, journal, rpc=None):
    executor = KeeperHubExecutor(
        client=transport, workflow_id='wf', wallet_address=WALLET, chain_id=CHAIN,
        rpc_url='http://synthetic.invalid', execution_profile='direct',
        submit_timeout=0.005, poll_interval=0)
    if rpc is not None:
        executor.reconciler.rpc = rpc
    executor.bind_journal(journal)
    return executor


def bind_receipt(executor, operation_id, transport, *, change=None):
    """A model KeeperHub record for this operation, and the chain row behind it."""
    payload = executor.prepare(ENV)[1]
    payload['operationId'] = 'a-different-operation' if change == 'operation' else operation_id
    if change == 'calldata':
        payload['functionArgs'][1] = 999
    transport.row = {'id': 'e', 'workflowId': 'wf', 'status': 'success', 'input': payload,
                     'transactionHashes': [{'hash': HASH, 'nodeId': 'action-1', 'chainId': CHAIN}]}
    transaction = {'hash': HASH, 'from': WALLET, 'to': payload['contractAddress'],
                   'input': encode_from_recorded_input(payload), 'value': hex(0)}

    async def rpc(method, params):
        if method == 'eth_chainId':
            return hex(CHAIN)
        if method == 'eth_getTransactionByHash':
            return dict(transaction)
        raise AssertionError(method)
    return rpc


@pytest.fixture
def diagnostic(tmp_path):
    database, operation = journal_with_one_pending(tmp_path)
    transport = Transport()
    journal = ReadOnlyJournal(database)
    seen = []
    watch(journal._conn, seen)
    yield dict(database=database, operation=operation, transport=transport,
               journal=journal, seen=seen,
               before_identity=identity(database), before_schema=schema(database))
    journal.close()


def assert_read_only(diagnostic):
    """Nothing was written: not by SQL, not to the schema, not to the operation."""
    assert [event for event in diagnostic['seen'] if event[1]] == []
    assert identity(diagnostic['database']) == diagnostic['before_identity']
    assert schema(diagnostic['database']) == diagnostic['before_schema']
    assert diagnostic['transport'].sends == 0


# --- the lifecycle the executor requires -------------------------------------

@pytest.mark.asyncio
async def test_an_empty_history_is_indeterminate_and_the_listing_is_read(diagnostic):
    """The diagnosis completes and is honest: nothing seen is not nothing sent."""
    executor = build(diagnostic['transport'], diagnostic['journal'])
    try:
        result, evidence = await executor._lookup_with_evidence(diagnostic['operation'])
    finally:
        await executor.close()

    assert result.outcome.value == 'indeterminate'
    assert result.txn_hash is None
    assert diagnostic['transport'].listings == 1, 'the listing must actually be read'
    assert evidence.executions_returned == 0
    assert_read_only(diagnostic)


@pytest.mark.asyncio
async def test_the_lifecycle_hook_does_not_replace_the_error_being_reported(diagnostic):
    """`lookup` calls the hook while handling a failure; it must not raise its own.

    Without the hook the operator loses the real reason and is told the adapter
    has no attribute instead.
    """
    diagnostic['transport'].listing_error = asyncio.CancelledError('diagnostic cancelled')
    executor = build(diagnostic['transport'], diagnostic['journal'])
    try:
        with pytest.raises(asyncio.CancelledError):
            await executor.lookup(diagnostic['operation'])
    finally:
        await executor.close()
    assert_read_only(diagnostic)


@pytest.mark.asyncio
async def test_validate_result_refuses_an_unproven_hash_on_its_own_terms(diagnostic):
    """The public API refuses with the SDK's own error, not an adapter defect."""
    executor = build(diagnostic['transport'], diagnostic['journal'])
    try:
        with pytest.raises(ExecutionOutcomeUnknownError):
            await executor.validate_result(diagnostic['operation'], HASH)
    finally:
        await executor.close()
    assert_read_only(diagnostic)


def test_the_hook_forgets_authority_the_view_never_grants(diagnostic):
    """It is an explicit no-op, and deliberately not the writer's implementation.

    A verifying journal drops its single-use permission here. This view issues
    none, so there is nothing to drop — which is a reason to answer, not a
    reason to inherit a writer that could also begin and record.
    """
    journal = diagnostic['journal']
    assert not isinstance(journal, OperationJournal)
    for _ in range(3):
        assert journal.invalidate_validation() is None
    assert not hasattr(journal, 'begin') and not hasattr(journal, 'record_hash')
    assert_read_only(diagnostic)


# --- reading only ------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_bound_receipt_is_landed_without_touching_the_journal(diagnostic):
    """A correct answer may be `landed`; it still must not record anything."""
    executor = build(diagnostic['transport'], diagnostic['journal'])
    rpc = bind_receipt(executor, diagnostic['operation'], diagnostic['transport'])
    executor.reconciler.rpc = rpc
    try:
        result, evidence = await executor._lookup_with_evidence(diagnostic['operation'])
    finally:
        await executor.close()

    assert result.outcome.value == 'landed' and result.txn_hash == HASH
    assert evidence.verification.get('localAuthorization') is True
    # This path does reach LocalAuthorization, so the observer was watching a
    # real query here: "no write" means something it cannot mean on an empty
    # history, where the journal is never asked anything at all.
    assert diagnostic['seen'], 'the adapter was never queried; the check would be vacuous'
    assert_read_only(diagnostic)
    row, = identity(diagnostic['database'])
    assert row['state'] == 'pending' and row['txn_hash'] is None and not row['consumed']


@pytest.mark.asyncio
@pytest.mark.parametrize('change,reason,journal_is_consulted', [
    ('operation', 'another operation id', False),
    ('calldata', 'a recorded envelope the SDK never authorized', True),
])
async def test_a_record_that_is_not_this_operation_is_not_adopted(
        diagnostic, change, reason, journal_is_consulted):
    """Two different refusals, refusing in two different places.

    A record carrying another operation id never matches, so the journal is not
    consulted at all. A record that matches this operation but disagrees with the
    envelope is refused by ``LocalAuthorization``, which does read the journal —
    and must still write nothing to it.
    """
    executor = build(diagnostic['transport'], diagnostic['journal'])
    executor.reconciler.rpc = bind_receipt(
        executor, diagnostic['operation'], diagnostic['transport'], change=change)
    try:
        result, evidence = await executor._lookup_with_evidence(diagnostic['operation'])
    finally:
        await executor.close()

    assert result.outcome.value == 'indeterminate', reason
    assert result.txn_hash is None
    assert bool(diagnostic['seen']) is journal_is_consulted
    if journal_is_consulted:
        assert any('authorization' in error.lower() or 'envelope' in error.lower()
                   for error in evidence.errors), evidence.errors
    assert_read_only(diagnostic)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['listing_error', 'logs_error'])
async def test_a_history_that_cannot_be_read_is_never_permission_to_resend(diagnostic, failure):
    """An unreadable source is uncertainty; `never_seen` would license a resend."""
    executor = build(diagnostic['transport'], diagnostic['journal'])
    if failure == 'logs_error':
        executor.reconciler.rpc = bind_receipt(
            executor, diagnostic['operation'], diagnostic['transport'])
    setattr(diagnostic['transport'], failure, RuntimeError('source unavailable'))
    try:
        result, evidence = await executor._lookup_with_evidence(diagnostic['operation'])
    finally:
        await executor.close()

    assert result.outcome.value == 'indeterminate'
    assert any('unavailable' in error for error in evidence.errors), evidence.errors
    assert_read_only(diagnostic)


def test_sqlite_itself_refuses_a_write_on_the_diagnostic_connection(diagnostic):
    with pytest.raises(sqlite3.OperationalError):
        diagnostic['journal']._conn.execute('UPDATE operations SET consumed=1')
    assert identity(diagnostic['database']) == diagnostic['before_identity']


def test_the_observer_used_here_can_see_a_write(tmp_path):
    """The control for the checks above: the authorizer does report an INSERT."""
    writer = OperationJournal(tmp_path / 'writer.sqlite')
    seen = []
    watch(writer._conn, seen)
    try:
        writer.begin(build_envelope({**ENV, 'value': 1}))
    finally:
        writer.close()
    assert 'SQLITE_INSERT' in {event[1] for event in seen}


def test_a_missing_journal_is_reported_not_created(tmp_path):
    absent = tmp_path / 'sdk-journal.sqlite'
    with pytest.raises(sqlite3.OperationalError):
        ReadOnlyJournal(absent)
    assert not absent.exists()
    assert list(tmp_path.iterdir()) == []


def test_an_incompatible_schema_is_reported_not_migrated(tmp_path):
    database = tmp_path / 'sdk-journal.sqlite'
    seed = sqlite3.connect(database)
    seed.execute('CREATE TABLE operations (operation_id TEXT)')
    seed.execute("INSERT INTO operations VALUES ('only-a-column')")
    seed.commit()
    seed.close()
    before = schema(database)

    journal = ReadOnlyJournal(database)
    try:
        with pytest.raises(Exception):  # noqa: PT011 - any refusal, never a migration
            journal.entries()
    finally:
        journal.close()
    assert schema(database) == before
