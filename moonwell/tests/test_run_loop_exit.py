"""Outer process status for the actual run_loop main/argparse resume branch.

Synthetic run writers are real. Chain, strategy and wiring are explicit offline
boundaries; the resumed iteration alone returns or raises. No SDK recovery claim
is made by these launcher controls.
"""
import json
import subprocess
import sys
from pathlib import Path
import pytest

@pytest.mark.parametrize('case,scenario,expected', [('success','resume',0),('failure','resume',1),('unknown','resume',1),('empty','resume',1),('unknown','recheck',0),('continue-failure','resume',1)])
def test_outer_exit_and_evidence(tmp_path,case,scenario,expected):
    result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--bootstrap',str(tmp_path),case,scenario],capture_output=True,text=True,timeout=40)
    (tmp_path/'child.log').write_text(result.stdout+result.stderr)
    (tmp_path/'child-result.json').write_text(json.dumps(dict(command=result.args,exit=result.returncode)))
    evidence=json.loads((tmp_path/'result.json').read_text())
    boundary=json.loads((tmp_path/'boundary.json').read_text())
    assert boundary['lock_reopened'] and boundary['executor_closed'] and boundary['client_closed']
    assert evidence['submits_this_run']==0 and boundary['sends']==0
    assert boundary['journal_before']==boundary['journal_after']
    if case in {'failure','unknown'}:
        assert evidence['result'][0] is False
        assert ('ExecutionOutcomeUnknownError' if case=='unknown' else 'RuntimeError') in evidence['resume_error']
    elif case=='success':assert evidence['result'][0] is True
    elif case=='continue-failure':assert evidence['result'][0] is True and evidence['continue_result'][0] is False
    else:assert evidence['result']==[False,'nothing in flight']
    assert result.returncode==expected, (result.returncode,expected,result.stderr)


def bootstrap(directory,case,scenario):
    import asyncio,runpy
    from types import SimpleNamespace
    from unittest.mock import patch
    from test_status_readonly import build_run,FakeChain
    from moonwell_demo.plan import RunPlan
    from wayfinder_paths.core.utils.executor import OperationJournal,ExecutionOutcomeUnknownError
    root=Path(__file__).resolve().parents[2];directory=Path(directory);state=directory/'state'
    build_run(state)
    if case=='empty':
        plan=RunPlan(state/'run-plan.json');plan.finish_iteration(0,'done');plan.close()
    journal=OperationJournal(state/'sdk-journal.sqlite');before=journal.entries();boundary={'sends':0,'executor_closed':False,'client_closed':False}
    class Executor:
        submits=0;preflights=[]
        async def close(self):boundary['executor_closed']=True
        async def submit(self,*a,**kw):boundary['sends']+=1;raise AssertionError('resume control must never submit')
    class Client:
        async def close(self):boundary['client_closed']=True
    async def setup():pass
    async def borrowable():return True,2*10**18
    strategy=SimpleNamespace(setup=setup,moonwell_adapter=SimpleNamespace(get_borrowable_amount=borrowable))
    async def iteration(amount):
        assert amount==10**18
        if case=='failure':raise RuntimeError('synthetic resume failure')
        if case=='unknown':raise ExecutionOutcomeUnknownError('synthetic unresolved original operation')
        return 2*10**18
    async def continuing(*a):return False,'synthetic continuation refusal'
    workflow=directory/'workflow.txt';workflow.write_text('synthetic-workflow')
    actual_run=asyncio.run
    def enter(coro):
        g=coro.cr_frame.f_globals
        g.update(Chain=FakeChain,WF_FILE=workflow,EV=directory/'evidence',DurableIteration=lambda *a:None,instrument=lambda *a,**kw:iteration,continue_loop=continuing)
        with patch.object(g['wiring'],'build',lambda **kw:(strategy,None,Executor(),Client(),journal)),patch.object(g['wiring'],'register_demo_abis',lambda:{}),patch.dict(g,{'install_stubs':lambda **kw:SimpleNamespace(lifi=SimpleNamespace(calls=[]),token=SimpleNamespace(calls=[]))}):
            return actual_run(coro)
    sys.argv=['run_loop.py',scenario,'--state-dir',str(state),'--out',str(directory/'result.json')]
    if case=='continue-failure':sys.argv.append('--continue-loop')
    try:
        with patch('asyncio.run',enter):runpy.run_path(str(root/'moonwell/scripts/run_loop.py'),run_name='__main__')
    finally:
        boundary['journal_before']=before;boundary['journal_after']=journal.entries();journal.close()
        plan=RunPlan(state/'run-plan.json');plan.close();boundary['lock_reopened']=True
        import hashlib
        boundary['launcher_sha256']=hashlib.sha256((root/'moonwell/scripts/run_loop.py').read_bytes()).hexdigest()
        boundary['origins']={name:dict(path=str(Path(module.__file__).resolve()),sha256=hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()) for name,module in sys.modules.copy().items() if name.split('.')[0] in {'moonwell_demo','keeperhub_executor','wayfinder_paths','operator_console'} and getattr(module,'__file__',None)}
        assert all(Path(item['path']).is_relative_to(root) for item in boundary['origins'].values())
        (directory/'boundary.json').write_text(json.dumps(boundary,indent=2))

if __name__=='__main__':
    assert sys.argv[1]=='--bootstrap'
    bootstrap(*sys.argv[2:])
