"""Real driver + upstream strategy + Moonwell/BRAP adapters + SDK + KeeperHub.

Only HTTP/RPC, token details, quotes, allowance/position reads and receipt polling
are modeled. Encoding, approval, swap, borrow/wrap/lend and journal paths are real.
"""
import copy
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from web3 import AsyncWeb3
from eth_abi import decode
from keeperhub_executor.executor import KeeperHubExecutor
from keeperhub_executor.client import ExecuteOutcome
from moonwell_demo.plan import RunPlan, CheckpointError
from moonwell_demo.durable import DurableIteration
from moonwell_demo.wiring import register_demo_abis, demo_abi_resolver, WALLET, WETH, WSTETH, M_WETH, M_WSTETH
from wayfinder_paths.core.utils.executor import ExternalExecution, OperationJournal
from wayfinder_paths.core.utils.wallets import get_external_executor_callback
from wayfinder_paths.core.utils import transaction, tokens
from wayfinder_paths.adapters.moonwell_adapter.adapter import MoonwellAdapter
from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter
from wayfinder_paths.strategies.moonwell_wsteth_loop_strategy.strategy import MoonwellWstethLoopStrategy

ROUTER = '0x1111111111111111111111111111111111111111'
AMOUNT = 10**18


class World:
    def __init__(self, crash=None, window='response'):
        self.crash, self.window = crash, window
        self.rows, self.txs, self.receipts, self.sends = [], {}, {}, []
        self.allowances = {}
        self.lent = 0
        self.eth = 0
        self.weth = 0
        self.wsteth = 0
        self.quotes = 0
        self.reset = False
        self.swap_entries = 0
        self.allowance_reads = []
        self.expiry = time.time() + 3600
        self.fired = False
        self.after_resume = False

    async def execute(self, wf, payload, *, idempotency_key):
        from keeperhub_executor.executor import encode_from_recorded_input, ether_string_to_wei
        fn = payload['abiFunction']
        kind = {'deposit':'wrap', 'mint':'lend', 'transfer':'swap', 'enterMarkets':'collateral'}.get(fn, fn)
        data = encode_from_recorded_input(payload)
        assert idempotency_key not in [r['operation_id'] for r in self.sends]
        row = next(r for r in self.sdk.journal.entries() if r['operation_id'] == idempotency_key)
        assert row['state'] == 'pending' and row['envelope']['data'] == data
        step = self.sdk.journal._conn.execute('SELECT step_id FROM execution_steps WHERE operation_id=?',(idempotency_key,)).fetchone()
        assert step and self.plan.data['run_id'] in step[0]
        h = '0x' + format(len(self.sends)+1, '064x')
        args=payload['functionArgs']
        boundary = kind
        if kind == 'approve':
            boundary = 'lend-approve' if payload['contractAddress'].lower() == WSTETH.lower() else 'approve'
            if int(args[1]) == 0:
                boundary += '-reset'
        self.sends.append(dict(step=kind, boundary=boundary, step_id=step[0], operation_id=idempotency_key, hash=h))
        if kind == 'borrow': self.eth += int(args[0])
        if kind == 'wrap':
            amount=ether_string_to_wei(payload['ethValue']); self.eth -= amount; self.weth += amount
        if kind == 'approve': self.allowances[(payload['contractAddress'].lower(), str(args[0]).lower())] = int(args[1])
        if kind == 'swap': self.wsteth = AMOUNT // 2; self.weth = 0
        if kind == 'lend': self.lent += int(args[0]); self.wsteth -= int(args[0])
        e = dict(id=f'e{len(self.rows)}', workflowId='wf', status='success', input=copy.deepcopy(payload),
                 transactionHashes=[dict(hash=h,nodeId='action-1',chainId=8453)])
        self.rows.append(e)
        self.txs[h] = dict(hash=h, **{'from':WALLET},to=payload['contractAddress'],input=data,
                          value=hex(ether_string_to_wei(payload['ethValue'])))
        self.receipts[h] = dict(transactionHash=h,status=1,blockNumber=len(self.sends))
        if boundary == self.crash and self.window == 'response' and not self.fired:
            self.fired=True
            self.hidden = h
            raise TimeoutError('accepted model operation; response lost')
        return ExecuteOutcome('started',200,{},e['id'])

    async def executions(self, wf):
        return [copy.deepcopy(r) for r in self.rows if r['transactionHashes'][0]['hash'] != getattr(self,'hidden',None)]

    async def execution_logs(self, eid):
        r=next(r for r in self.rows if r['id']==eid);h=r['transactionHashes'][0]['hash']
        return dict(execution=copy.deepcopy(r), logs=[dict(executionId=eid,nodeId='action-1',
            nodeType='web3/write-contract',status='success',output=dict(transactionHash=h,chainId=8453))])

    async def rpc(self, method, params):
        if method=='eth_chainId': return hex(8453)
        if method=='eth_getTransactionByHash': return copy.deepcopy(self.txs.get(params[0]))
        if method=='eth_getTransactionReceipt': return copy.deepcopy(self.receipts.get(params[0]))
        raise AssertionError(method)

    async def receipt(self, chain, h, **kw):
        kind=next(r['boundary'] for r in self.sends if r['hash']==h)
        if kind==self.crash and self.window=='receipt' and not self.fired:
            self.fired=True
            raise CheckpointError('receipt known; driver result not saved')
        return copy.deepcopy(self.receipts[h])

    async def balance(self, *, token_id, wallet_address, block_identifier=None):
        if token_id == 'ethereum-base':
            if block_identifier is not None:
                # Historical borrow block is stable even after later wrap/change.
                return int(next(r for r in self.rows if r['input']['abiFunction']=='borrow')['input']['functionArgs'][0])
            return self.eth
        if token_id == 'l2-standard-bridged-weth-base-base': return 0 if block_identifier is not None else self.weth
        return self.wsteth

    async def open(self, path, monkeypatch, *, start=False):
        register_demo_abis()
        self.plan=RunPlan(path/'plan.json')
        if start: self.plan.start_iteration(AMOUNT,2*AMOUNT)
        journal=OperationJournal(path/'sdk.sqlite')
        ex=KeeperHubExecutor(client=self,workflow_id='wf',wallet_address=WALLET,chain_id=8453,
            rpc_url='http://offline.invalid',abi_resolver=demo_abi_resolver,execution_profile='direct',submit_timeout=.003,poll_interval=0)
        ex.reconciler.rpc=self.rpc
        sdk=self.sdk=ExternalExecution(ex,journal)
        callback=get_external_executor_callback(sdk)
        # The actual strategy constructor builds actual adapters; reads are replaced below.
        strategy=MoonwellWstethLoopStrategy({'main_wallet':{'address':WALLET},'strategy_wallet':{'address':WALLET}},
            main_wallet_signing_callback=callback,strategy_wallet_signing_callback=callback)
        assert isinstance(strategy.moonwell_adapter,MoonwellAdapter)
        assert isinstance(strategy.brap_adapter,BRAPAdapter)
        original_swap = strategy._swap_with_retries
        async def counted_swap(*args, **kwargs):
            self.swap_entries += 1
            return await original_swap(*args, **kwargs)
        strategy._swap_with_retries = counted_swap
        strategy._get_balance_raw=self.balance
        strategy._gas_keep_wei=lambda: 0
        strategy.balance_adapter.get_balance=AsyncMock(side_effect=lambda **kw:(True,self.wsteth))
        strategy.moonwell_adapter.get_pos=AsyncMock(side_effect=lambda **kw:(True,{'mtoken_balance':self.lent}))
        strategy.moonwell_adapter.is_market_entered=AsyncMock(return_value=(True,True))
        async def quote(**kw):
            self.quotes+=1
            amount=int(kw['amount'])
            # Minimal router quote; real BRAP adapter emits the approval and swap.
            return True, {'input_amount':amount,'output_amount':AMOUNT//2,
                'quote': {'minAmountOut': str(AMOUNT//2), 'createdAt': 1},
                'expires_at': self.expiry,
                'calldata': {'to':WETH,'data':'0xa9059cbb'+ROUTER[2:].rjust(64,'0')+format(amount + self.quotes - 1,'064x'),'value':0}}
        strategy.brap_adapter.best_quote=quote
        strategy.brap_adapter._record_swap_operation=AsyncMock(return_value={})
        from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
        async def details(token_id):
            return {'address': WETH if 'weth' in token_id and 'wsteth' not in token_id else WSTETH,
                    'chain':{'id':8453},'decimals':18,'symbol':'TEST'}
        monkeypatch.setattr(TOKEN_CLIENT,'get_token_details',details)
        @asynccontextmanager
        async def web3(chain): yield AsyncWeb3()
        monkeypatch.setattr(transaction,'web3_from_chain_id',web3)
        monkeypatch.setattr(tokens,'web3_from_chain_id',web3)
        import wayfinder_paths.adapters.moonwell_adapter.adapter as moon_module
        @asynccontextmanager
        async def membership_web3(chain):
            fn=SimpleNamespace(call=AsyncMock(return_value=True))
            yield SimpleNamespace(eth=SimpleNamespace(contract=lambda **kw: SimpleNamespace(functions=SimpleNamespace(checkMembership=lambda *a:fn))))
        monkeypatch.setattr(moon_module,'web3_from_chain_id',membership_web3)
        monkeypatch.setattr(transaction,'wait_for_transaction_receipt',self.receipt)
        async def allowance(token,chain,owner,spender):
            self.allowance_reads.append((token.lower(), spender.lower()))
            return self.allowances.get((token.lower(),spender.lower()),0)
        monkeypatch.setattr(tokens,'get_token_allowance',allowance)
        if self.reset:
            monkeypatch.setattr(tokens, 'TOKENS_REQUIRING_APPROVAL_RESET',
                                {(8453, WETH), (8453, WSTETH)})
        DurableIteration(strategy,sdk,self.plan)
        return strategy

    async def close(self):
        self.sdk.journal.close(); await self.sdk.executor.close(); self.plan.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('window',['response','receipt','checkpoint','driver'])
@pytest.mark.parametrize('step',['borrow','wrap','approve','lend-approve','approve-reset','lend-approve-reset','swap','lend','collateral'])
async def test_recovery_boundaries(tmp_path,monkeypatch,step,window):
    world=World(step,window)
    world.reset = step.endswith('-reset')
    strategy=await world.open(tmp_path,monkeypatch,start=True)
    if window=='driver':
        original_flush=world.plan._flush
        call_name={'swap':'_swap_with_retries','wrap':'wrap_eth','collateral':'set_collateral'}.get(step,step)
        def driver_flush():
            if not world.sends or world.sends[-1]['boundary'] != step:
                return original_flush()
            leaf = world.sends[-1]['step_id'].removesuffix('/send/0')
            if any((k == leaf if 'approve' in step else k.endswith(f'/{call_name}/0'))
                   and r['state']=='done' for k,r in world.plan.data['calls'].items()) and not world.fired:
                world.fired=True
                raise CheckpointError('adapter result known before durable driver checkpoint')
            original_flush()
        world.plan._flush=driver_flush
    if window=='checkpoint':
        original=world.sdk.journal.record_hash
        def flush(*args, **kwargs):
            original(*args, **kwargs)
            if world.sends and world.sends[-1]['boundary']==step and not world.fired:
                # Simulate process loss after adapter result but before durable file replace.
                world.fired=True
                raise CheckpointError('driver checkpoint not committed')
        world.sdk.journal.record_hash=flush
    with pytest.raises(CheckpointError): await strategy._atomic_deposit_iteration(AMOUNT)
    assert world.fired and world.sends[-1]['boundary']==step
    before=copy.deepcopy(world.sends)
    await world.close()
    world.hidden=None;world.after_resume=True
    # State deliberately changes; completed read results must survive restart.
    world.eth += 1234
    strategy=await world.open(tmp_path,monkeypatch)
    try:
        amount=await strategy._atomic_deposit_iteration(AMOUNT)
    finally:
        assert [s['step'] for s in world.sends].count('borrow')==1, world.sends
    world.plan.finish_iteration(amount,'done')
    assert amount==AMOUNT//2 and world.lent==AMOUNT//2
    kinds = [s['step'] for s in world.sends]
    assert kinds.count('borrow') == kinds.count('wrap') == kinds.count('swap') == kinds.count('lend') == kinds.count('collateral') == 1
    assert kinds.count('approve') == (4 if world.reset else 2)
    assert len({r['boundary'] for r in world.sends if r['step'] == 'approve'}) == kinds.count('approve')
    assert world.swap_entries == 1  # never re-enter the upstream retry loop
    assert world.quotes==1
    assert [s['operation_id'] for s in world.sends[:len(before)]]==[s['operation_id'] for s in before]
    bindings = dict(world.sdk.journal._conn.execute('SELECT step_id, operation_id FROM execution_steps'))
    assert world.sends[:len(before)] == before
    assert all(bindings[r['step_id']] == r['operation_id'] for r in before)
    assert all(r['state']!='orphaned' for r in world.sdk.journal.entries())
    await world.close()


def test_checkpoint_seed_lock_legacy_corruption(tmp_path):
    path=tmp_path/'plan.json';plan=RunPlan(path)
    plan.record_seed(10);assert not plan.seed_done
    with pytest.raises(CheckpointError,match='writer'): RunPlan(path)
    plan.finish_seed();assert plan.seed_done;plan.close()
    loaded=RunPlan(path);assert loaded.seed_done;loaded.close()
    for text in ['{', '{}', '{"seed":null,"iterations":[]}',json.dumps({'data':{'version':2},'sha256':'bad'})]:
        path.write_text(text)
        with pytest.raises(CheckpointError,match='invalid or legacy'): RunPlan(path)


@pytest.mark.asyncio
async def test_step_identity_and_equal_envelopes(tmp_path,monkeypatch):
    world=World();strategy=await world.open(tmp_path,monkeypatch,start=True)
    adapter=strategy.moonwell_adapter
    # Real adapter + SDK. Separate identities may legally have identical bytes.
    prefix=world.plan.data['run_id']
    for key in [prefix+'/1/borrow',prefix+'/2/borrow']:
        with world.sdk.step(key): await adapter.borrow(mtoken=M_WETH,amount=AMOUNT)
    before=copy.deepcopy(world.sends)
    with world.sdk.step(prefix+'/1/borrow'):
        await adapter.borrow(mtoken=M_WETH,amount=AMOUNT)
    assert world.sends==before and len(world.sends)==2
    with pytest.raises(ValueError,match='identity/envelope'):
        with world.sdk.step(prefix+'/1/borrow'):
            await adapter.borrow(mtoken=M_WETH,amount=AMOUNT+1)
    assert world.sends==before
    await world.close()


@pytest.mark.asyncio
async def test_missing_checkpoint_cannot_restart_existing_sdk_run(tmp_path, monkeypatch):
    world = World('borrow', 'response')
    strategy = await world.open(tmp_path, monkeypatch, start=True)
    with pytest.raises(CheckpointError):
        await strategy._atomic_deposit_iteration(AMOUNT)
    before = copy.deepcopy(world.sends)
    await world.close()
    (tmp_path / 'plan.json').unlink()
    for _ in range(2):
        with pytest.raises(CheckpointError, match='another SDK run'):
            await world.open(tmp_path, monkeypatch)
        assert world.sends == before
        await world.close()


def test_second_process_cannot_open_run(tmp_path):
    import subprocess
    import sys
    plan = RunPlan(tmp_path / 'plan.json')
    p = subprocess.run([sys.executable, '-c',
        'from moonwell_demo.plan import RunPlan; import sys; RunPlan(sys.argv[1])', str(plan.path)],
        capture_output=True, text=True)
    assert p.returncode == 1 and 'run already has a writer' in p.stderr
    plan.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('resolution', ['unknown', 'landed'])
async def test_expired_saved_quote(tmp_path, monkeypatch, resolution):
    world = World('swap', 'response')
    strategy = await world.open(tmp_path, monkeypatch, start=True)
    with pytest.raises(CheckpointError):
        await strategy._atomic_deposit_iteration(AMOUNT)
    before = copy.deepcopy(world.sends)
    quote_key = next(k for k in world.plan.data['calls'] if k.endswith('/best_quote'))
    saved = copy.deepcopy(world.plan.data['calls'][quote_key]['result'])
    await world.close()
    # Advance time without rewriting the economic quote or its checkpoint.
    monkeypatch.setattr('moonwell_demo.durable.time.time', lambda: world.expiry + 1)
    if resolution == 'landed':
        world.hidden = None
    strategy = await world.open(tmp_path, monkeypatch)
    if resolution == 'unknown':
        for _ in range(2):
            with pytest.raises(CheckpointError):
                await strategy._atomic_deposit_iteration(AMOUNT)
            assert world.sends == before
    else:
        assert await strategy._atomic_deposit_iteration(AMOUNT) == AMOUNT // 2
        assert [r['step'] for r in world.sends].count('swap') == 1
    assert world.quotes == 1
    assert world.plan.data['calls'][quote_key]['result'] == saved
    await world.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('window', ['response', 'done'])
async def test_allowance_effect_is_not_operation_ownership(tmp_path, monkeypatch, window):
    world = World('approve' if window == 'response' else 'swap', 'response')
    strategy = await world.open(tmp_path, monkeypatch, start=True)
    with pytest.raises(CheckpointError):
        await strategy._atomic_deposit_iteration(AMOUNT)
    before = copy.deepcopy(world.sends)
    approve = next(r for r in before if r['step'] == 'approve')
    assert world.allowances[(WETH.lower(), WETH.lower())] > 0
    if window == 'done':
        # A completed allowance group must still require its exact child operation.
        world.sdk.journal._conn.execute('DELETE FROM execution_steps WHERE operation_id=?',
                                        (approve['operation_id'],))
        world.hidden = None
    await world.close()
    strategy = await world.open(tmp_path, monkeypatch)
    reads = len(world.allowance_reads)
    for _ in range(2):
        with pytest.raises(CheckpointError):
            await strategy._atomic_deposit_iteration(AMOUNT)
        assert world.sends == before
        assert len(world.allowance_reads) == reads
        assert world.quotes == 1
    await world.close()


@pytest.mark.asyncio
async def test_quote_received_before_checkpoint_is_not_requested_again(tmp_path, monkeypatch):
    world = World()
    strategy = await world.open(tmp_path, monkeypatch, start=True)
    flush = world.plan._flush
    def lose_quote_checkpoint():
        if any(k.endswith('/best_quote') and r['state'] == 'done'
               for k, r in world.plan.data['calls'].items()):
            raise CheckpointError('quote response received before checkpoint')
        flush()
    world.plan._flush = lose_quote_checkpoint
    with pytest.raises(CheckpointError):
        await strategy._atomic_deposit_iteration(AMOUNT)
    before = copy.deepcopy(world.sends)
    await world.close()
    strategy = await world.open(tmp_path, monkeypatch)
    with pytest.raises(CheckpointError, match='no automatic second quote'):
        await strategy._atomic_deposit_iteration(AMOUNT)
    assert world.sends == before and world.quotes == 1
    await world.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('validity', ['expired', 'unknown'])
async def test_saved_quote_without_swap_binding_stops(tmp_path, monkeypatch, validity):
    world = World('approve', 'response')
    if validity == 'unknown':
        world.expiry = None
    strategy = await world.open(tmp_path, monkeypatch, start=True)
    with pytest.raises(CheckpointError):
        await strategy._atomic_deposit_iteration(AMOUNT)
    before = copy.deepcopy(world.sends)
    await world.close()
    world.hidden = None
    if validity == 'expired':
        monkeypatch.setattr('moonwell_demo.durable.time.time', lambda: world.expiry + 1)
    strategy = await world.open(tmp_path, monkeypatch)
    with pytest.raises(CheckpointError, match='expired or validity unknown'):
        await strategy._atomic_deposit_iteration(AMOUNT)
    assert world.sends == before and world.quotes == 1
    await world.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('observed,reset,expected', [(100, False, []), (0, False, [200]), (1, True, [0, 200])])
async def test_allowance_without_durable_driver(monkeypatch, observed, reset, expected):
    async def signer(tx):
        raise AssertionError('the transport is modeled; no signing')
    values = []
    async def build(**kwargs):
        return kwargs
    async def send(tx, callback, **kwargs):
        assert callback is signer
        values.append(tx['amount'])
        return '0x' + 'ab' * 32
    async def read(*args):
        return values[-1] if values else observed
    monkeypatch.setattr(tokens, 'get_token_allowance', read)
    monkeypatch.setattr(tokens, 'build_approve_transaction', build)
    monkeypatch.setattr(tokens, 'send_transaction', send)
    monkeypatch.setattr(tokens, 'TOKENS_REQUIRING_APPROVAL_RESET', {(8453, WETH)} if reset else set())
    result = await tokens.ensure_allowance(token_address=WETH, owner=WALLET, spender=ROUTER,
        amount=100, chain_id=8453, signing_callback=signer, approval_amount=200)
    assert result == ((True, '0x' + 'ab' * 32) if expected else (True, {}))
    assert values == expected
