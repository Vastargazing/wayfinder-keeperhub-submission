"""Checkpoint the real strategy's decision boundaries without replacing its math.

Cached reads preserve pre-borrow balances and completed monetary call results.
Named child calls preserve quotes, allowance decisions and individual sends.
The upstream retry loop is never replayed after an incomplete swap.
"""
import contextvars
import copy
import functools
import json
import math
import time
from types import SimpleNamespace

from .plan import CheckpointError


def portable(value):
    return json.loads(json.dumps(value))


class DurableIteration:
    def __init__(self, strategy, execution, plan):
        self.strategy, self.execution, self.plan = strategy, execution, plan
        self.scope = contextvars.ContextVar('driver_call', default=None)
        conn = execution.journal._conn
        table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='moonwell_run'").fetchone()
        if not table:
            if execution.journal.entries() or plan.data['calls']:
                raise CheckpointError('legacy SDK journal has no driver run binding')
            conn.execute('CREATE TABLE moonwell_run (singleton INTEGER PRIMARY KEY CHECK(singleton=1), run_id TEXT NOT NULL)')
            conn.execute('INSERT INTO moonwell_run VALUES (1,?)', (plan.data['run_id'],))
        else:
            row = conn.execute('SELECT run_id FROM moonwell_run WHERE singleton=1').fetchone()
            if row is None or row[0] != plan.data['run_id']:
                raise CheckpointError('checkpoint missing or belongs to another SDK run')
        self.real_iteration = strategy._atomic_deposit_iteration
        self.gas_keep = strategy._gas_keep_wei
        def gas_keep():
            if self.scope.get() is None:
                return self.gas_keep()
            key = self.scope.get()[0].split('/iteration/')[0] + '/gas-reserve'
            calls = plan.data['calls']
            if key not in calls:
                calls[key] = {'intent': {}, 'state': 'done', 'result': int(self.gas_keep())}
                plan._flush()
            return calls[key]['result']
        strategy._gas_keep_wei = gas_keep
        for name in ('_get_balance_raw',):
            self.wrap(strategy, name, money=False)
        self.wrap(strategy, '_swap_with_retries', money=True, replay=False, composite=True, resume=self.resume_swap)
        self.wrap(strategy.brap_adapter, 'swap_from_token_ids', money=True, composite=True, named=True)
        self.wrap(strategy.brap_adapter, 'swap_from_quote', money=True, composite=True, named=True)
        self.wrap(strategy.brap_adapter, 'best_quote', money=False, named=True, once=True)
        for name in ('borrow', 'wrap_eth', 'lend', 'set_collateral', 'repay', 'unlend'):
            self.wrap(strategy.moonwell_adapter, name, money=True, replay=name in {'borrow', 'wrap_eth'},
                      composite=name in {'lend', 'set_collateral'})
        self.wrap(strategy.moonwell_adapter, 'get_pos', money=False)
        self.wrap(strategy.balance_adapter, 'get_balance', money=False)
        strategy._atomic_deposit_iteration = self.run
        # Unknown sends must escape upstream's broad retry/rollback handlers.
        original_send = execution.send

        async def guarded_send(transaction):
            if self.scope.get() is None:
                return await original_send(transaction)
            try:
                return await original_send(transaction)
            except Exception as exc:
                raise CheckpointError(f'SDK send stopped: {exc}') from exc
        async def named_send(transaction):
            if self.scope.get() is None or execution._step.get() is not None:
                return await guarded_send(transaction)
            await self.check_swap_validity()
            return await self.subcall('transaction', lambda: guarded_send(transaction),
                                      intent=transaction, money=True)
        execution.send = named_send
        execution.durable_call = self.subcall

    async def subcall(self, name, action, *, intent, money=False, composite=False):
        async def invoke(**kwargs):
            return await action()
        obj = SimpleNamespace(**{name: invoke})
        self.wrap(obj, name, money=money, replay=money, composite=composite, named=True)
        return await getattr(obj, name)(**intent)

    async def validate_completed(self, key, *, money, composite):
        calls = self.plan.data['calls']
        leaves = [k for k, r in calls.items() if k.startswith(key + '/')
                  and r.get('money') and not r.get('composite')]
        if not composite:
            leaves = [key]
        if money and not leaves:
            raise CheckpointError('completed driver call lacks its SDK operations')
        for leaf in leaves:
            # Exact step identity, never a matching envelope or observed effect.
            table = self.execution.journal._conn.execute(
                "SELECT name FROM sqlite_master WHERE name='execution_steps'").fetchone()
            bound = table and self.execution.journal._conn.execute(
                'SELECT operation_id FROM execution_steps WHERE step_id=?',
                (leaf + '/send/0',)).fetchone()
            operations = {r['operation_id']: r for r in self.execution.journal.entries()}
            operation = operations.get(bound[0]) if bound else None
            if (calls[leaf]['state'] != 'done' or operation is None
                    or operation['state'] != 'submitted' or not operation['txn_hash']):
                raise CheckpointError('completed driver call lacks its SDK operations')
            try:
                await self.execution._validate_result(operation['operation_id'], operation['txn_hash'])
            except Exception as exc:
                raise CheckpointError(f'completed operation cannot be validated: {exc}') from exc

    async def resume_swap(self, key):
        child = self.plan.data['calls'].get(key + '/swap_from_token_ids')
        if child is None:
            raise CheckpointError('incomplete swap has no saved adapter intent; operator reconciliation required')
        intent = child['intent']
        success, result = await self.strategy.brap_adapter.swap_from_token_ids(
            *intent['args'], **intent['kwargs'])
        if not success or not result:
            raise CheckpointError('saved swap failed; no retry or fallback')
        return result if isinstance(result, dict) else {'to_amount': result if isinstance(result, int) else 0}

    async def check_swap_validity(self):
        path = self.scope.get()[0]
        if not path.endswith('/swap_from_quote'):
            return
        quote_row = self.plan.data['calls'][path.rsplit('/', 1)[0] + '/best_quote']
        expiry = quote_row.get('expires_at')
        # A bound operation can only be resolved/revalidated by SDK send. Even
        # an expired quote may recover its original landed hash; it cannot resend.
        table = self.execution.journal._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='execution_steps'").fetchone()
        bound = table and self.execution.journal._conn.execute(
            'SELECT operation_id FROM execution_steps WHERE step_id=?',
            (path + '/transaction/send/0',)).fetchone()
        if bound:
            return
        if (expiry is not None and time.time() >= expiry) or (expiry is None and quote_row.get('resumed')):
            raise CheckpointError('saved quote expired or validity unknown; operator decision required')

    def wrap(self, obj, name, *, money, replay=False, composite=False, named=False, once=False, resume=None):
        original = getattr(obj, name)

        @functools.wraps(original)
        async def call(*args, **kwargs):
            parent = self.scope.get()
            if parent is None:
                return await original(*args, **kwargs)
            path, counters = parent
            index = counters.get(name, 0)
            counters[name] = index + 1
            if named and index:
                raise CheckpointError(f'named substep called twice; no retry: {path}/{name}')
            key = f'{path}/{name}' if named else f'{path}/{name}/{index}'
            intent = portable({'args': args, 'kwargs': kwargs})
            row = self.plan.data['calls'].get(key)
            if row is not None:
                if row['intent'] != intent:
                    raise CheckpointError(f'call identity/arguments conflict: {key}')
                if once:
                    row['resumed'] = True
                if row['state'] == 'done':
                    if money or composite:
                        await self.validate_completed(key, money=money, composite=composite)
                    return copy.deepcopy(row['result'])
                if once:
                    raise CheckpointError('quote request already started; no automatic second quote')
                if money and not replay and not composite:
                    raise CheckpointError(f'incomplete stateful call requires reconciliation: {key}')
            continuing = row is not None
            if row is None:
                row = {'intent': intent, 'state': 'started', 'children_version': 1,
                       'money': money, 'composite': composite}
                self.plan.data['calls'][key] = row
                self.plan._flush()
            token = self.scope.set((key, {}))
            try:
                if continuing and composite and row.get('children_version') != 1:
                    raise CheckpointError('legacy incomplete stateful call requires reconciliation')
                if name == 'swap_from_quote':
                    await self.check_swap_validity()
                if continuing and resume is not None and not replay:
                    result = await resume(key)
                elif money and not composite:
                    with self.execution.step(key):
                        result = await original(*args, **kwargs)
                else:
                    result = await original(*args, **kwargs)
                if name == 'borrow' and result[0] and isinstance(result[1], str):
                    txn_hash = result[1]
                    receipt = await self.execution.executor.reconciler.rpc('eth_getTransactionReceipt', [txn_hash])
                    if (not receipt or str(receipt.get('transactionHash', '')).lower() != txn_hash.lower()
                            or int(str(receipt.get('status')), 0) != 1):
                        raise CheckpointError('borrow has no confirmed successful receipt')
                    block = receipt['blockNumber']
                    block = int(block, 16) if isinstance(block, str) else int(block)
                    result = [True, {'transactionHash': txn_hash, 'confirmed_block_number': block}]
                result = portable(result)
                if once:
                    if not result or not result[0] or not isinstance(result[1], dict):
                        raise CheckpointError('quote unavailable; no automatic second quote')
                    quote = result[1]
                    metadata = quote.get('quote') or {}
                    # The pinned BRAP schema specifies createdAt, not a TTL.
                    # Never invent validity from createdAt. Preserve the full
                    # quote and explicit provider expiry; unknown stays unknown.
                    expiry = quote.get('expires_at', quote.get('expiresAt', metadata.get('deadline')))
                    expiry = float(expiry) if expiry is not None else None
                    if expiry is not None and (not math.isfinite(expiry) or expiry <= 0):
                        raise CheckpointError('invalid quote expiry; operator decision required')
                    row['expires_at'] = expiry
                    row['received_at'] = time.time()
                if (money or composite) and (result is None or (isinstance(result, list) and result[0] is False)):
                    raise CheckpointError(f'monetary call failed; no automatic rollback: {key}')
                row.update(state='done', result=result)
                self.plan._flush()
                return copy.deepcopy(result)
            except Exception as exc:
                raise CheckpointError(f'call stopped: {key}: {exc}') from exc
            finally:
                self.scope.reset(token)
        setattr(obj, name, call)

    async def run(self, borrow_amt_wei):
        r = self.plan.in_flight
        if r is None or r['borrow_amt_wei'] != borrow_amt_wei:
            raise CheckpointError('iteration intent missing or changed')
        root = f"{self.plan.data['run_id']}/iteration/{r['index']}"
        token = self.scope.set((root, {}))
        try:
            return await self.real_iteration(borrow_amt_wei)
        finally:
            self.scope.reset(token)
