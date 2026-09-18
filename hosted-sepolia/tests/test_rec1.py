import asyncio,json,sqlite3
from copy import deepcopy
import pytest
from tests.test_provenance import prepared
from tests.test_recovery import close,envelope,World
from tests.fixtures import HASH,CHAIN_ID
from wayfinder_paths.core.utils.executor import ExecutionOutcomeUnknownError

async def setup(tmp_path,transition):
 w,ex,op,lg=await prepared(tmp_path)
 if transition=='consume': await ex.resolve_pending(CHAIN_ID)
 await ex.executor.validate_result(op,HASH)
 return w,ex,op,lg

def transition(ex,op,kind,h=HASH,env=None):
 if kind=='record': return ex.journal.record_hash(op,h,consumed=False)
 return ex.journal.consume_claim(env or envelope('erc20_mint'),op,h)

@pytest.mark.parametrize('kind',['record','consume'])
@pytest.mark.parametrize('change',['hash_arg','op_arg','digest','envelope','sender','chain_id','txn_hash','consumed','proof','updated_at','state'])
async def test_binding_change_refuses_without_writes(tmp_path,kind,change):
 w,ex,op,_=await setup(tmp_path,kind)
 args={}
 if change=='hash_arg': args['h']='0x'+'cd'*32
 elif change=='op_arg': op='absent-operation'
 elif change=='proof': ex.journal._conn.execute("UPDATE hosted_verification SET proof=?",(json.dumps({'ok':True}),))
 else:
  values={'digest':'drift','envelope':json.dumps(envelope('supply')),'sender':'other','chain_id':1,'txn_hash':'0x'+'cd'*32,'consumed':1,'updated_at':1,'state':'orphaned'}
  ex.journal._conn.execute(f'UPDATE operations SET {change}=?',(values[change],))
 before=ex.journal.entries()
 with pytest.raises((ValueError,ExecutionOutcomeUnknownError)): transition(ex,op,kind,**args)
 assert ex.journal.entries()==before and len(before)==1
 with pytest.raises((ValueError,ExecutionOutcomeUnknownError)): transition(ex,op,kind)
 assert ex.journal.entries()==before and w.posts==[] and ex.executor.submits==0
 await close(ex)

@pytest.mark.parametrize('kind',['record','consume'])
async def test_permission_once_and_fresh_validation_needed(tmp_path,kind):
 w,ex,op,_=await setup(tmp_path,kind)
 transition(ex,op,kind)
 before=ex.journal.entries()
 with pytest.raises(ValueError): transition(ex,op,kind)
 assert ex.journal.entries()==before
 if kind=='record':
  with pytest.raises(ValueError): transition(ex,op,'consume')
  await ex.executor.validate_result(op,HASH)
  transition(ex,op,'consume')
 assert ex.journal.entries()[0]['consumed']
 await close(ex)

@pytest.mark.parametrize('failure',['malformed_send','wrong_validate_hash','missing_operation','http_exception','rpc_exception','cancel','direct_verify_refusal'])
@pytest.mark.parametrize('kind',['record','consume'])
async def test_new_attempt_revokes_even_before_verify(tmp_path,kind,failure):
 w,ex,op,lg=await setup(tmp_path,kind)
 before=ex.journal.entries()
 if failure=='malformed_send':
  with pytest.raises(ValueError): await ex.send({})
 elif failure=='wrong_validate_hash':
  with pytest.raises(ExecutionOutcomeUnknownError): await ex.executor.validate_result(op,'0x'+'cd'*32)
 elif failure=='missing_operation': assert (await ex.executor.lookup('missing')).outcome.value=='indeterminate'
 elif failure=='http_exception':
  async def unavailable(*args): raise RuntimeError('modeled listing failure')
  ex.executor.client.executions=unavailable
  assert (await ex.executor.lookup(op)).outcome.value=='indeterminate'
 elif failure in {'rpc_exception','cancel'}:
  async def unavailable(*args):
   if failure=='cancel': raise asyncio.CancelledError()
   raise RuntimeError('modeled RPC failure')
  ex.executor.reconciler.rpc=unavailable
  if failure=='cancel':
   with pytest.raises(asyncio.CancelledError): await ex.executor.lookup(op)
  else: assert (await ex.executor.lookup(op)).outcome.value=='indeterminate'
 else:
  gate=ex.executor.verification_gate
  result=await gate.verify(op,HASH,{},None,[],w.rpc,{})
  assert not result['ok']
 with pytest.raises(ValueError): transition(ex,op,kind)
 assert ex.journal.entries()==before and w.posts==[]
 await close(ex)

async def test_suspended_attempt_cannot_grant_after_new_failure(tmp_path):
 w,ex,op,_=await setup(tmp_path,'record')
 entered=asyncio.Event(); resume=asyncio.Event()
 async def rpc(method,params):
  if method=='eth_getTransactionReceipt': entered.set(); await resume.wait()
  return await w.rpc(method,params)
 ex.executor.reconciler.rpc=rpc
 old=asyncio.create_task(ex.executor.lookup(op)); await entered.wait()
 assert (await ex.executor.lookup('missing')).outcome.value=='indeterminate'
 resume.set(); assert (await old).outcome.value=='indeterminate'
 before=ex.journal.entries()
 with pytest.raises(ValueError): transition(ex,op,'record')
 assert ex.journal.entries()==before
 await close(ex)

@pytest.mark.parametrize('kind',['record','consume'])
async def test_other_connection_consumed_refuses(tmp_path,kind):
 w,ex,op,_=await setup(tmp_path,kind)
 c=sqlite3.connect(ex.journal.path,isolation_level=None)
 c.execute('UPDATE operations SET consumed=1 WHERE operation_id=?',(op,));c.close()
 before=ex.journal.entries()
 with pytest.raises(ValueError): transition(ex,op,kind)
 assert ex.journal.entries()==before and before[0]['consumed']
 await close(ex)

async def test_legacy_schema_preserves_accepted_observation(tmp_path):
 w,ex,op,_=await setup(tmp_path,'record')
 tables=ex.journal._conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
 saved=json.loads(ex.journal._proof_row(op)[2]); accepted=saved.pop('acceptedObservation')
 ex.journal.save_proof(op,HASH,saved['authorizationDigest'],saved)
 await close(ex)
 ex=await w.open(tmp_path,allow=False)
 with pytest.raises(ValueError): transition(ex,op,'record')
 w.receipts[HASH]['status']='0x0'
 assert (await ex.executor.lookup(op)).outcome.value=='indeterminate'
 old=json.loads(ex.journal._proof_row(op)[2])
 assert old.pop('acceptedObservation')==accepted and old==saved
 assert ex.journal.observations(op)[0]['accepted'] is False
 assert ex.journal._conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()==tables
 assert ex.journal.entries()[0]['state']=='pending'
 await close(ex)

async def test_real_new_send_has_two_verifications_and_second_failure_keeps_proof(tmp_path):
 w=World();ex=await w.open(tmp_path)
 calls=0
 async def rpc(method,params):
  nonlocal calls
  value=await w.rpc(method,params)
  if method=='eth_getTransactionReceipt':
   calls+=1
   if calls==2: raise ValueError('model receipt unavailable on second check')
  return value
 ex.executor.reconciler.rpc=rpc
 with pytest.raises(ExecutionOutcomeUnknownError): await ex.send(envelope())
 assert calls==2 and len(w.posts)==ex.executor.submits==1
 op=ex.journal.entries()[0]['operation_id']
 assert ex.journal.entries()[0]['state']=='pending' and json.loads(ex.journal._proof_row(op)[2])['ok']
 with pytest.raises(ValueError): transition(ex,op,'record',h=next(iter(w.txs)))
 ex.executor.reconciler.rpc=w.rpc
 assert await ex.send(envelope())==next(iter(w.txs))
 assert len(w.posts)==1 and ex.journal.entries()[0]['consumed']
 await close(ex)

async def test_durable_step_repeat_reads_without_reconsumption(tmp_path):
 w=World();ex=await w.open(tmp_path)
 with ex.step('model-step'): h=await ex.send(envelope())
 before=ex.journal.entries()
 with ex.step('model-step'): assert await ex.send(envelope())==h
 assert ex.journal.entries()==before and len(w.posts)==1
 with pytest.raises(ValueError): ex.journal.record_hash(before[0]['operation_id'],h,consumed=True)
 assert ex.journal.entries()==before
 await close(ex)

@pytest.mark.parametrize('kind',['record','consume'])
async def test_sql_lock_refusal_spends_permission(tmp_path,kind):
 w,ex,op,_=await setup(tmp_path,kind)
 before=ex.journal.entries()
 ex.journal._conn.execute('PRAGMA busy_timeout=1')
 other=sqlite3.connect(ex.journal.path,isolation_level=None)
 other.execute('BEGIN IMMEDIATE')
 with pytest.raises(sqlite3.OperationalError):transition(ex,op,kind)
 other.execute('ROLLBACK');other.close()
 with pytest.raises(ValueError):transition(ex,op,kind)
 assert ex.journal.entries()==before
 await close(ex)

async def test_sql_failure_rolls_back_observation_and_preserves_proof(tmp_path):
 w,ex,op,lg=await setup(tmp_path,'record')
 before=ex.journal.entries();proof=ex.journal._proof_row(op);obs=ex.journal.observations(op)
 ex.journal._conn.execute("CREATE TRIGGER reject_proof BEFORE INSERT ON hosted_verification BEGIN SELECT RAISE(ABORT,'synthetic proof storage failure'); END")
 assert (await ex.executor.lookup(op)).outcome.value=='indeterminate'
 assert ex.journal._proof_row(op)==proof and ex.journal.observations(op)==obs
 with pytest.raises(ValueError):transition(ex,op,'record')
 assert ex.journal.entries()==before
 await close(ex)

async def test_rpc_never_runs_inside_write_transaction(tmp_path):
 w,ex,op,_=await setup(tmp_path,'record')
 calls=[]
 async def rpc(method,params):
  assert not ex.journal._conn.in_transaction
  calls.append(method);return await w.rpc(method,params)
 ex.executor.reconciler.rpc=rpc
 await ex.executor.validate_result(op,HASH)
 assert calls==['eth_getTransactionByHash','eth_getTransactionReceipt','eth_chainId']
 transition(ex,op,'record')
 await close(ex)

async def test_permission_cannot_move_to_another_existing_operation(tmp_path):
 w,ex,op,lg=await prepared(tmp_path)
 other=ex.journal.begin(envelope('supply'))
 await ex.executor.validate_result(op,HASH)
 before=ex.journal.entries()
 with pytest.raises(ValueError):ex.journal.record_hash(other,HASH,consumed=False)
 with pytest.raises(ValueError):ex.journal.record_hash(op,HASH,consumed=False)
 assert ex.journal.entries()==before and w.posts==[]
 await close(ex)

async def test_permission_does_not_accept_another_caller_envelope(tmp_path):
 w,ex,op,_=await setup(tmp_path,'consume')
 before=ex.journal.entries()
 with pytest.raises(ValueError):ex.journal.consume_claim(envelope('supply'),op,HASH)
 with pytest.raises(ValueError):ex.journal.consume_claim(envelope('erc20_mint'),op,HASH)
 assert ex.journal.entries()==before and w.posts==[]
 await close(ex)
