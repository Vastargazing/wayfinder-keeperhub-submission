"""Offline injected-boundary tests, using the real SDK journal/execution seam.

Fake HTTP/chain records exercise decision logic; they are not KeeperHub runtime
proof, signer counts, or network idempotency evidence.
"""
import asyncio
from contextlib import closing
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / 'wayfinder' / 'upstream')]
from keeperhub_executor.executor import KeeperHubExecutor, ether_string_to_wei, wei_to_ether_string
from keeperhub_executor.client import ExecuteOutcome
from keeperhub_executor.reconcile import ChainMatch
from wayfinder_paths.core.utils.executor import ExternalExecution, OperationJournal, ExecutionOutcomeUnknownError

WALLET = '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266'
TOKEN = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
POOL = '0xA238Dd80C259a72e81d7e4664a9801593F98d1c5'
HASH = '0x' + 'a1' * 32
OTHER = '0x' + 'b2' * 32
ENV = dict(chainId=8453, **{'from': WALLET}, to=TOKEN, value=0,
    data='0x095ea7b3' + POOL[2:].lower().rjust(64, '0') + hex(123)[2:].rjust(64, '0'))

class Client:
    def __init__(self):
        self.rows = []; self.logs = []; self.posts = 0; self.starts = 0; self.keys = {}
        self.delayed = False
    async def executions(self, wf): return copy.deepcopy(self.rows)
    async def execution_logs(self, eid): return {'execution': copy.deepcopy(next(r for r in self.rows if r['id'] == eid)), 'logs': copy.deepcopy(self.logs)}
    async def execute(self, wf, payload, *, idempotency_key):
        self.posts += 1
        if self.delayed:
            self.held = (wf, copy.deepcopy(payload), idempotency_key)
            raise TimeoutError('POST held before server received it; lookup sees absence')
        if idempotency_key in self.keys:
            assert self.keys[idempotency_key] == payload
            return ExecuteOutcome('replay', 200, {'idempotentReplay': True}, 'e', True)
        self.keys[idempotency_key] = copy.deepcopy(payload); self.starts += 1
        return ExecuteOutcome('started', 200, {}, 'e')

class Hardening(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = Client()
        self.ex = KeeperHubExecutor(execution_profile="direct",
        client=self.client, workflow_id='wf', wallet_address=WALLET,
            chain_id=8453, rpc_url='http://localhost:1', submit_timeout=.005, poll_interval=0)
        self.addAsyncCleanup(self.ex.close)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.journal = OperationJournal(Path(self.tmp.name) / 'sdk.sqlite'); self.addCleanup(self.journal.close)
        op = self.journal.begin(ENV)
        self.journal._conn.execute("UPDATE operations SET operation_id='B' WHERE operation_id=?", (op,))
        self.ex.bind_journal(self.journal)
        self.payload = self.ex.prepare(ENV)[1]; self.payload['operationId'] = 'B'
        self.row = dict(id='e', workflowId='wf', status='success', input=self.payload,
            transactionHashes=[dict(hash=HASH, nodeId='action-1', chainId=8453)])
        self.client.rows = [self.row]
        self.client.logs = [dict(executionId='e', nodeId='action-1', nodeType='web3/write-contract',
            status='success', output=dict(transactionHash=HASH, chainId=8453))]
        async def rpc(method, params):
            if method == 'eth_chainId': return hex(8453)
            assert method == 'eth_getTransactionByHash'
            return dict(hash=HASH, **{'from': WALLET}, to=TOKEN, input=ENV['data'], value='0x0')
        self.ex.reconciler.rpc = AsyncMock(side_effect=rpc)
        self.ex._block_window = AsyncMock(return_value=(1, 10))
        self.ex.reconciler.find = AsyncMock(return_value=([], {'from_block':1,'to_block':10,'complete':True}))

    async def answer(self):
        result, self.last_evidence = await self.ex._lookup_with_evidence('B')
        return result.outcome.value

    async def test_logs_fixture_and_direct_control_reach_rpc(self):
        self.assertEqual(await self.answer(), 'landed')
        self.assertEqual(self.last_evidence.logs_execution, self.row)
        self.assertTrue(self.last_evidence.verification['localAuthorization'])
        self.ex.reconciler.rpc.assert_any_await('eth_getTransactionByHash', [HASH])
        self.client.logs[0]['output']['transactionHash'] = OTHER
        self.assertEqual(await self.answer(), 'indeterminate')
        self.assertIn('Conflicting operation-bound hashes', self.last_evidence.detail)
        self.assertEqual(self.last_evidence.logs_execution, self.row)

    async def test_old_identical_a_never_adopted_for_b(self):
        self.row['status'] = 'error'; self.row['transactionHashes'] = []; self.client.logs = []
        old = ChainMatch(HASH, 5, 7, 1)
        self.ex.reconciler.find.return_value = ([old], {'from_block':1,'to_block':10,'complete':True})
        answer, ev = await self.ex._lookup_with_evidence('B')
        self.assertEqual(answer.outcome.value, 'indeterminate'); self.assertIsNone(answer.txn_hash)
        self.assertEqual(ev.chain_matches[0]['hash'], HASH)
        self.client.rows = []
        self.assertEqual(await self.answer(), 'indeterminate')

    async def test_empty_or_unavailable_db_never_authorizes_resend_with_optout(self):
        self.client.rows = []
        for option in (True, False):
            for probe in (None, {}, {'status':'processing'}):
                with self.subTest(option=option, probe=probe):
                    self.ex.require_db_probe = option
                    self.ex.db_probe = AsyncMock(); self.ex.db_probe.record.return_value = probe
                    self.assertEqual(await self.answer(), 'indeterminate')
        self.ex.db_probe = None
        self.assertEqual(await self.answer(), 'indeterminate')

    async def test_delayed_old_post_after_absence_no_second_sdk_submit(self):
        self.client.rows = []; self.client.logs = []; self.client.delayed = True
        self.ex.db_probe = AsyncMock(); self.ex.db_probe.record.return_value = {}
        with tempfile.TemporaryDirectory() as d, closing(OperationJournal(Path(d)/'journal.sqlite')) as journal:
            self.ex.authorization = None
            sdk = ExternalExecution(self.ex, journal)
            with self.assertRaises(ExecutionOutcomeUnknownError): await sdk.send(ENV)
            op = journal.entries()[0]['operation_id']
            with self.assertRaises(ExecutionOutcomeUnknownError): await sdk.send(ENV)
            self.assertEqual(self.client.posts, 1); self.assertEqual(journal.entries()[0]['state'],'pending')
            # Deliver the held original request only after the empty lookup.
            self.client.delayed = False
            self.row['input']['operationId'] = op; self.client.rows = [self.row]
            self.ex.db_probe = None
            adopted = await sdk.send(ENV)
            self.assertEqual(adopted, HASH); self.assertEqual(self.client.posts, 1)
            self.assertEqual(len(journal.entries()), 1)

    async def test_same_operation_replay_mock_contract(self):
        a = await self.ex.submit(ENV, operation_id='B')
        b = await self.ex.submit(ENV, operation_id='B')
        self.assertEqual((a,b),(HASH,HASH)); self.assertEqual(self.client.posts,2)
        self.assertEqual(self.client.starts,1)  # FakeClient contract, not live KH proof.

    async def test_status_matrix(self):
        for status in ('pending','running','unconfirmed','success','error','skipped','cancelled',
                       'phantom','system_error','queued','timeout','failed','future','',None):
            with self.subTest(status=status):
                self.row['status'] = status
                expected = 'landed' if status in {'success','error','system_error','cancelled'} else 'indeterminate'
                self.assertEqual(await self.answer(), expected)
        self.row['transactionHashes'] = []; self.client.logs = []
        for status in ('pending','running','unconfirmed','success','error','skipped','cancelled','phantom','system_error'):
            self.row['status'] = status
            self.assertEqual(await self.answer(), 'indeterminate')

    async def test_reclaimable_system_error_not_terminal_for_adoption(self):
        self.row['status'] = 'system_error'
        for code in ('P-0001','P-0005'):
            self.row['errorCode'] = code
            self.assertEqual(await self.answer(),'indeterminate')
        self.row['errorCode'] = 'E-0001'
        self.assertEqual(await self.answer(),'landed')

    async def test_conflicts_all_sources_and_outputraw(self):
        self.client.logs[0]['output']['transactionHash'] = OTHER
        self.assertEqual(await self.answer(), 'indeterminate')
        self.client.logs[0]['output']['transactionHash'] = HASH
        self.client.logs[0]['outputRaw'] = dict(transactionHash=OTHER, chainId=8453)
        self.assertEqual(await self.answer(), 'indeterminate')
        self.client.logs[0].pop('outputRaw')
        self.ex.db_probe = AsyncMock()
        self.ex.db_probe.pending_transactions.return_value = [dict(execution_id='e',wallet_address=WALLET,
            chain_id=8453,nonce=7,tx_hash=OTHER,status='pending')]
        self.assertEqual(await self.answer(), 'indeterminate')

    async def test_wrong_provenance_fails_closed(self):
        row = copy.deepcopy(self.row); log = copy.deepcopy(self.client.logs[0])
        for key, val in [('workflowId','other'),('id',None)]:
            self.row[key] = val; self.assertEqual(await self.answer(),'indeterminate'); self.row.update(copy.deepcopy(row))
        for key, val in [('network','1'),('ethValue','1e2'),('contractAddress','garbage')]:
            self.row['input'][key] = val; self.assertEqual(await self.answer(),'indeterminate'); self.row.update(copy.deepcopy(row))
        for key,val in [('nodeId','other'),('chainId',1),('hash','0xdead')]:
            self.row['transactionHashes'][0][key] = val
            self.assertEqual(await self.answer(),'indeterminate'); self.row.update(copy.deepcopy(row))
        for key,val in [('nodeId','other'),('nodeType','http'),('executionId','other'),('status','future'),('status','running')]:
            self.client.logs[0][key] = val
            self.assertEqual(await self.answer(),'indeterminate'); self.client.logs[0] = copy.deepcopy(log)
        self.client.logs[0]['output']['chainId'] = 1
        self.assertEqual(await self.answer(),'indeterminate')

    async def test_wrong_pending_attribution(self):
        self.ex.db_probe = AsyncMock()
        original = dict(execution_id='e',wallet_address=WALLET,chain_id=8453,nonce=7,tx_hash=HASH,status='pending')
        for key,val in [('execution_id','x'),('wallet_address',TOKEN),('chain_id',1),('nonce',-1),('status','dropped'),('status','replaced')]:
            self.ex.db_probe.pending_transactions.return_value = [{**original,key:val}]
            self.assertEqual(await self.answer(),'indeterminate')
        self.ex.db_probe.pending_transactions.return_value = [original]
        self.assertEqual(await self.answer(),'landed')

    async def test_missing_or_mismatched_attributed_tx_fails_closed(self):
        for tx in (None, dict(hash=HASH, **{'from':TOKEN}, to=TOKEN,input=ENV['data'],value='0x0')):
            self.ex.reconciler.rpc = AsyncMock(return_value=tx)
            self.assertEqual(await self.answer(),'indeterminate')

    async def test_user_error_and_clean_scan_not_quiescence(self):
        self.row['status']='error'; self.row['transactionHashes']=[]
        self.client.logs[0]['status']='error'
        self.client.logs[0]['output']={'errorClass':'user','error':'invalid ABI','chainId':8453}
        self.assertEqual(await self.answer(),'indeterminate')

    async def test_multiple_runs_same_id(self):
        self.client.rows.append(copy.deepcopy(self.row))
        self.assertEqual(await self.answer(),'indeterminate')

    async def test_http404_and_unavailable_logs_fail_closed(self):
        self.client.executions = AsyncMock(side_effect=RuntimeError('HTTP 404'))
        self.assertEqual(await self.answer(),'indeterminate')
        self.client.executions = AsyncMock(return_value=[self.row])
        self.client.execution_logs = AsyncMock(side_effect=RuntimeError('HTTP 503'))
        self.assertEqual(await self.answer(),'indeterminate')

    async def test_exact_nonzero_value_reaches_diagnostic_scanner(self):
        self.row['status']='error'; self.row['transactionHashes']=[]; self.client.logs=[]
        self.row['input']['ethValue']='1.000000000000000001'
        self.assertEqual(await self.answer(),'indeterminate')
        self.assertEqual(self.ex.reconciler.find.call_args.kwargs['value'],10**18+1)

class Values(unittest.TestCase):
    def test_extremes_exact_both_directions(self):
        for n in (0,1,10**18-1,10**18,10**18+1,12345678901234567890,2**128+1,2**256-1):
            self.assertEqual(ether_string_to_wei(wei_to_ether_string(n)),n)
        self.assertEqual(ether_string_to_wei('.000000000000000001'),1)
        self.assertEqual(ether_string_to_wei('1.000000000000000001000'),10**18+1)
    def test_reject_inexact_or_out_of_range(self):
        for s in ('1e2','NaN','Infinity','-1',' 1','+1','1.0000000000000000001',1.1,True,str(2**256)):
            with self.subTest(s=s),self.assertRaises(ValueError): ether_string_to_wei(s)
        for n in (-1,2**256):
            with self.assertRaises(ValueError): wei_to_ether_string(n)

if __name__ == '__main__': unittest.main(verbosity=2)
