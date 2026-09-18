"""Run every command and retain the first failure, through actual main/argparse."""
import json
import os
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import offline_checks

@pytest.mark.parametrize('failures,expected',[({},0),({1:17},17),({4:17},17),({1:17,3:23},17),({0:23,8:17},23),({2:124,5:17},124)])
def test_all_commands_keep_first_failure(tmp_path,monkeypatch,failures,expected):
    calls=[]
    def observed(arguments,output,roots):
        i=len(calls);calls.append(arguments)
        code=failures.get(i,0)
        script='import time; time.sleep(5)' if code==124 else f'raise SystemExit({code})'
        return [sys.executable,'-c',script]
    monkeypatch.setattr(offline_checks,'observed_command',observed)
    monkeypatch.setattr(sys,'argv',['offline_checks.py','--out',str(tmp_path),'--timeout','hosted=0.1'])
    result=offline_checks.main()
    rows=json.loads((tmp_path/'commands.json').read_text())
    assert result==expected
    assert len(calls)==len(rows)==9
    assert [row['exit'] for row in rows]==[failures.get(i,0) for i in range(9)]
    assert all(not row['remaining_live_members'] for row in rows)
