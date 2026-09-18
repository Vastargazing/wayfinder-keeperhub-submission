"""Observe owned descendants, including nested mutation-style sessions."""
import ctypes
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import pytest

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
from process_control import run_process


def alive(item):
    try:
        f=Path(f"/proc/{item['pid']}/stat").read_text().rsplit(')',1)[1].split()
        return f[19]==item['start'] and f[0]!='Z'
    except FileNotFoundError:return False

@pytest.mark.parametrize('exit_code',[0,17])
def test_normal_and_nonzero(tmp_path,exit_code):
    with (tmp_path/'output.log').open('w') as log:
        result=run_process([sys.executable,'-c',f'raise SystemExit({exit_code})'],cwd=tmp_path,env=os.environ,log=log,timeout=3)
    assert result['exit']==exit_code and not result['timed_out']
    assert result['remaining_live_members']==[]

@pytest.mark.parametrize('nested,ignore_term',[(False,False),(False,True),(True,False),(True,True)])
def test_timeout_cleans_exact_grandchild(tmp_path,nested,ignore_term):
    libc=ctypes.CDLL(None);previous=ctypes.c_int();assert libc.prctl(37,ctypes.byref(previous),0,0,0)==0
    assert libc.prctl(36,1,0,0,0)==0
    identity=tmp_path/'identity.json';observed=[];stop=threading.Event()
    grandchild='''import os,json,signal,time
from pathlib import Path
'''+('signal.signal(signal.SIGTERM,signal.SIG_IGN)\n' if ignore_term else '')+f"Path({str(identity)!r}).write_text(json.dumps(dict(pid=os.getpid(),start=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])))\ntime.sleep(30)"
    child=f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{grandchild!r}]); time.sleep(30)"
    cmd=[sys.executable,'-c',child]
    if nested:
        code=f"import sys,os; sys.path.insert(0,{str(SCRIPTS)!r}); from process_control import run_process; log=open('nested.log','w'); run_process({cmd!r},cwd={str(tmp_path)!r},env=os.environ,log=log,timeout=20)"
        cmd=[sys.executable,'-c',code]
    def observer():
        while not stop.wait(.01):
            try:item=json.loads(identity.read_text())
            except (FileNotFoundError,ValueError):continue
            if alive(item):observed.append(item);return
    watcher=threading.Thread(target=observer);watcher.start()
    try:
        with (tmp_path/'output.log').open('w') as log:
            result=run_process(cmd,cwd=tmp_path,env=os.environ,log=log,timeout=1)
        assert observed,'positive observer never saw the exact live descendant'
        item=observed[0]
        assert result['exit']==124 and result['timed_out']
        assert not alive(item),item
        assert result['remaining_live_members']==[]
        (tmp_path/'result.json').write_text(json.dumps(dict(result=result,live_control=item,after_alive=alive(item)),indent=2))
    finally:
        stop.set();watcher.join(2)
        if identity.exists():
            item=json.loads(identity.read_text())
            if alive(item):os.kill(item['pid'],signal.SIGKILL)
        # Reap only children adopted by this probe, never signal other processes.
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            try:
                pid,_=os.waitpid(-1,os.WNOHANG)
                if pid==0:time.sleep(.01)
            except ChildProcessError:break
        assert libc.prctl(36,previous.value,0,0,0)==0
