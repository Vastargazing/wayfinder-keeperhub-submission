"""Real KeeperHub executor -> SDK journal transitions; transport is an offline model."""
import copy
from types import SimpleNamespace

import pytest
from keeperhub_executor.executor import KeeperHubExecutor
from keeperhub_executor.client import ExecuteOutcome
from wayfinder_paths.core.utils.executor import ExternalExecution, OperationJournal, ExecutionOutcomeUnknownError, build_envelope

WALLET = '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266'
TOKEN = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
OTHER = '0x4200000000000000000000000000000000000006'
POOL = '0xA238Dd80C259a72e81d7e4664a9801593F98d1c5'
HASH = '0x' + 'ab' * 32
ENV = build_envelope(dict(chainId=8453, **{'from': WALLET}, to=TOKEN, value=0,
    data='0x095ea7b3' + POOL[2:].lower().rjust(64, '0') + format(123, '064x')))


class Model:
    def __init__(self):
        self.posts = 0
        self.reads = []
        self.row = None
        self.change = None

    def observe(self, payload):
        payload = copy.deepcopy(payload)
        if self.change == 'calldata': payload['functionArgs'][1] = 999
        if self.change == 'target': payload['contractAddress'] = OTHER
        if self.change == 'value': payload['ethValue'] = '1'
        if self.change == 'chain': payload['network'] = '1'
        self.row = dict(id='e', workflowId='wf', status='success', input=payload,
            transactionHashes=[dict(hash=HASH, nodeId='action-1', chainId=8453)])
        from keeperhub_executor.executor import encode_from_recorded_input
        self.tx = dict(hash=HASH, **{'from': OTHER if self.change == 'sender' else WALLET},
                       to=payload['contractAddress'], input=encode_from_recorded_input(payload),
                       value=hex(10**18 if self.change == 'value' else 0))

    async def execute(self, wf, payload, *, idempotency_key):
        self.posts += 1
        assert self.sdk.journal.pending(WALLET, 8453)[0]['operation_id'] == idempotency_key
        self.observe(payload)
        return ExecuteOutcome('started', 200, {}, 'e')

    async def executions(self, wf):
        return [copy.deepcopy(self.row)] if self.row else []

    async def execution_logs(self, eid):
        self.reads.append('logs')
        return {'execution': copy.deepcopy(self.row), 'logs': [dict(executionId='e',
            nodeId='action-1', nodeType='web3/write-contract', status='success',
            output=dict(transactionHash=HASH, chainId=8453))]}

    async def rpc(self, method, params):
        self.reads.append(method)
        if method == 'eth_chainId': return hex(1 if self.change == 'chain' else 8453)
        if method == 'eth_getTransactionByHash': return copy.deepcopy(self.tx)
        if method == 'eth_getTransactionReceipt':
            return dict(transactionHash=HASH, status=0, blockNumber=1)
        raise AssertionError(method)


@pytest.fixture
async def system(tmp_path):
    m = Model()
    journal = OperationJournal(tmp_path / 'sdk.sqlite')
    ex = KeeperHubExecutor(client=m, workflow_id='wf', wallet_address=WALLET, chain_id=8453,
        rpc_url='http://offline.invalid', execution_profile='direct', submit_timeout=.005, poll_interval=0)
    ex.reconciler.rpc = m.rpc
    sdk = m.sdk = ExternalExecution(ex, journal)
    yield SimpleNamespace(m=m, ex=ex, sdk=sdk, journal=journal)
    journal.close()
    await ex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['submit', 'resolve', 'resume'])
@pytest.mark.parametrize('change', [None, 'calldata', 'target', 'value', 'sender', 'chain'])
async def test_authorization_lifecycle(system, stage, change):
    m, ex, sdk, journal = system.m, system.ex, system.sdk, system.journal
    m.change = change
    if stage != 'submit':
        op = journal.begin(ENV)
        payload = ex.prepare(ENV)[1]; payload['operationId'] = op
        m.observe(payload)
    async def attempt():
        if stage == 'resolve':
            await sdk.resolve_pending(8453)
        return await sdk.send(ENV)
    if change:
        with pytest.raises(ExecutionOutcomeUnknownError): await attempt()
        row = journal.entries()[0]
        assert row['state'] == 'pending' and row['txn_hash'] is None
        op = row['operation_id']
        answer = await ex.lookup(op)
        assert answer.outcome.value == 'indeterminate' and answer.txn_hash is None
        if change in {'calldata', 'target', 'value'}:
            # Both API snapshots passed their consistency checks; refusal is local authorization.
            result, ev = await ex._lookup_with_evidence(op)
            assert ev.logs_execution == m.row
            assert any('SDK authorization' in e for e in ev.errors), ev.as_dict()
        if change == 'sender': assert 'eth_getTransactionByHash' in m.reads
        with pytest.raises(ExecutionOutcomeUnknownError): await sdk.send(ENV)
        assert [r['operation_id'] for r in journal.entries()] == [op]
    else:
        assert await attempt() == HASH
        assert journal.entries()[0]['txn_hash'] == HASH
        assert 'eth_getTransactionByHash' in m.reads and 'eth_chainId' in m.reads
    assert m.posts == (1 if stage == 'submit' else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['other_operation', 'existing_hash', 'no_profile', 'no_journal'])
async def test_binding_constraints(system, kind):
    m, ex, sdk, journal = system.m, system.ex, system.sdk, system.journal
    op = journal.begin(ENV)
    payload = ex.prepare(ENV)[1]; payload['operationId'] = op; m.observe(payload)
    if kind == 'other_operation':
        other = journal.begin({**ENV, 'value': 1}); journal.record_hash(other, HASH, consumed=True)
    elif kind == 'existing_hash':
        journal._conn.execute('UPDATE operations SET txn_hash=? WHERE operation_id=?', ('0x'+'cd'*32, op))
    elif kind == 'no_profile': ex.execution_profile = None
    else: ex.authorization = None
    with pytest.raises(ExecutionOutcomeUnknownError): await sdk.resolve_pending(8453)
    assert next(r for r in journal.entries() if r['operation_id'] == op)['state'] == 'pending'
    assert m.posts == 0


@pytest.mark.asyncio
async def test_unconsumed_hash_revalidated(system):
    m, ex, sdk, journal = system.m, system.ex, system.sdk, system.journal
    op = journal.begin(ENV); payload = ex.prepare(ENV)[1]; payload['operationId'] = op; m.observe(payload)
    await sdk.resolve_pending(8453)
    m.change = 'calldata'; m.observe(payload)
    with pytest.raises(ExecutionOutcomeUnknownError): await sdk.send(ENV)
    assert m.posts == 0 and len(journal.entries()) == 1


@pytest.mark.asyncio
async def test_revert_hash_preserved_but_transaction_call_raises(system, monkeypatch):
    from wayfinder_paths.core.utils import transaction
    from wayfinder_paths.core.utils.wallets import get_external_executor_callback
    from unittest.mock import AsyncMock
    monkeypatch.setattr(transaction, 'wait_for_transaction_receipt', AsyncMock(return_value={'status': 0}))
    with pytest.raises(transaction.TransactionRevertedError) as caught:
        await transaction.send_transaction(ENV, get_external_executor_callback(system.sdk))
    assert caught.value.txn_hash == HASH
    assert system.journal.entries()[0]['txn_hash'] == HASH
    assert system.m.posts == 1


def reopened_system(model, path):
    journal = OperationJournal(path)
    executor = KeeperHubExecutor(client=model, workflow_id='wf', wallet_address=WALLET,
        chain_id=8453, rpc_url='http://offline.invalid', execution_profile='direct',
        submit_timeout=.005, poll_interval=0)
    executor.reconciler.rpc = model.rpc
    sdk = model.sdk = ExternalExecution(executor, journal)
    return journal, executor, sdk


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['unavailable', 'conflict', 'validator_exception'])
async def test_claim_revalidation_reopens_without_resend(tmp_path, failure):
    model = Model()
    path = tmp_path / 'sdk.sqlite'
    journal, executor, sdk = reopened_system(model, path)
    try:
        operation = journal.begin(ENV)
        payload = executor.prepare(ENV)[1]
        payload['operationId'] = operation
        model.observe(payload)
        await sdk.resolve_pending(8453)
        original = journal.entries()
        assert journal.claim(ENV) == (operation, HASH)
        assert journal.entries() == original  # candidate read is not consumption
    finally:
        journal.close()
        await executor.close()

    listing = model.executions
    async def unavailable(*args):
        raise TimeoutError('offline model: temporarily unavailable')
    if failure == 'unavailable':
        model.executions = unavailable
    elif failure == 'conflict':
        model.change = 'calldata'
        model.observe(payload)

    # Each refusal closes the database and discards both SDK and executor.
    for _ in range(3):
        journal, executor, sdk = reopened_system(model, path)
        try:
            if failure == 'validator_exception':
                executor.validate_result = unavailable
            with pytest.raises(ExecutionOutcomeUnknownError) as caught:
                await sdk.send(ENV)
            assert caught.value.operation_id == operation
            assert model.posts == 0
            assert journal.entries() == original
            assert journal.claim(ENV) == (operation, HASH)
            assert journal.entries() == original
        finally:
            journal.close()
            await executor.close()

    model.executions = listing
    model.change = None
    model.observe(payload)
    journal, executor, sdk = reopened_system(model, path)
    try:
        assert await sdk.send(ENV) == HASH
        (row,) = journal.entries()
        assert row['operation_id'] == operation
        assert row['state'] == 'submitted' and row['consumed']
        assert row['txn_hash'] == HASH
        assert model.posts == 0
    finally:
        journal.close()
        await executor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('competing_writer', [False, True])
async def test_claim_consumption_race(system, competing_writer):
    model, executor, sdk, journal = system.m, system.ex, system.sdk, system.journal
    operation = journal.begin(ENV)
    payload = executor.prepare(ENV)[1]
    payload['operationId'] = operation
    model.observe(payload)
    await sdk.resolve_pending(8453)
    validator = executor.validate_result
    accepted = []

    async def validate_then_compete(op, txn_hash):
        await validator(op, txn_hash)
        if competing_writer:
            # A second actual SDK/executor/SQLite connection wins during the await boundary.
            other_journal, other_executor, other_sdk = reopened_system(model, journal.path)
            try:
                accepted.append(await other_sdk.send(ENV))
            finally:
                other_journal.close()
                await other_executor.close()

    executor.validate_result = validate_then_compete
    if competing_writer:
        with pytest.raises(ExecutionOutcomeUnknownError) as caught:
            await sdk.send(ENV)
        assert caught.value.operation_id == operation
        assert 'consumed or changed' in str(caught.value)
        assert accepted == [HASH]
    else:
        assert await sdk.send(ENV) == HASH
    (row,) = journal.entries()
    assert row['operation_id'] == operation and row['consumed']
    assert model.posts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['pending', 'submitted'])
@pytest.mark.parametrize('named', [False, True])
async def test_unfinished_envelope_blocks_begin(system, monkeypatch, state, named):
    model, sdk, journal = system.m, system.sdk, system.journal
    resolve = sdk.resolve_pending
    seeded = []

    async def resolve_then_other_writer(chain):
        await resolve(chain)
        # An existing/imported older row, hidden by a newer completed envelope.
        # For pending, this also models a writer arriving after pending resolution.
        other = OperationJournal(journal.path)
        try:
            operation = other.begin(ENV)
            newer = other.begin({**ENV, 'value': 1})
            other.record_hash(newer, '0x' + 'cd' * 32, consumed=True)
            if state == 'submitted':
                other.record_hash(operation, HASH, consumed=False)
            other._conn.execute('UPDATE operations SET created_at=1 WHERE operation_id=?', (operation,))
            other._conn.execute('UPDATE operations SET created_at=2 WHERE operation_id=?', (newer,))
            seeded.extend(other.entries())
        finally:
            other.close()

    monkeypatch.setattr(sdk, 'resolve_pending', resolve_then_other_writer)
    begin_calls = []
    begin = journal.begin
    def observed_begin(envelope):
        begin_calls.append(envelope)
        return begin(envelope)
    monkeypatch.setattr(journal, 'begin', observed_begin)
    from contextlib import nullcontext
    with sdk.step('new-step') if named else nullcontext():
        with pytest.raises(ExecutionOutcomeUnknownError) as caught:
            await sdk.send(ENV)
    assert caught.value.operation_id == seeded[0]['operation_id']
    assert 'unfinished operation' in str(caught.value)
    assert begin_calls == []
    assert model.posts == 0
    assert journal.entries() == seeded
