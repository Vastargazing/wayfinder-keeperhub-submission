from pathlib import Path
import subprocess,sys,json,hashlib,importlib,importlib.metadata,os,site
import argparse
p=argparse.ArgumentParser(description='Real CLI with synthetic state and an explicit model Chain; no RPC')
p.add_argument('--out',type=Path,required=True);a=p.parse_args()
C=Path(__file__).resolve().parents[2];O=a.out.resolve();O.mkdir(parents=True,exist_ok=False);state=O/'fixture';rows=[]
def snap():
 return {q.name:dict(sha256=hashlib.sha256(q.read_bytes()).hexdigest(),size=q.stat().st_size,mtime_ns=q.stat().st_mtime_ns) for q in state.iterdir() if q.is_file()} if state.exists() else {}
def run(label,args,expected):
 p=subprocess.run([sys.executable,*map(str,args)],capture_output=True,text=True,timeout=120);(O/(label+'.stdout')).write_text(p.stdout);(O/(label+'.stderr')).write_text(p.stderr);rows.append(dict(label=label,args=list(map(str,args)),exit=p.returncode,expected=expected));(O/'commands.json').write_text(json.dumps(rows,indent=2));assert p.returncode==expected,(label,p.stderr)
run('fixture',['-m','operator_console','--make-fixture',state],0);before=snap()
run('repeat',['-m','operator_console','--make-fixture',state],2);repeat=snap();assert before==repeat
run('render',['-m','operator_console','--state-dir',state,'--manifest',state/'run-manifest.json','--render',O/'console.html'],0);render=snap()
run('missing-status',['moonwell/scripts/run_loop.py','status','--state-dir',O/'missing','--out',O/'missing.json'],2);assert not (O/'missing').exists()
run('status',['-c',(C/'integration/scripts/snapshot_status_model.py').read_text(),'status','--state-dir',state,'--out',O/'reports/status.json'],0);status=snap()
from operator_console.sources import load_state_directory
manifest=load_state_directory(state,O/'reports/status.json').manifest
assert manifest.bound,manifest.binding_detail
report=json.loads((O/'reports/status.json').read_text());assert report['run_id']=='f'*32
assert ((O/'reports')/report['stateDir']).resolve()==state
run('manifest-render',['-m','operator_console','--state-dir',state,'--manifest',O/'reports/status.json','--render',O/'status-console.html'],0)
for stage in [render,status,snap()]:
 for name in before:assert before[name]==stage[name],(name,'primary file drift')
(O/'CHECK.json').write_text(json.dumps(dict(before=before,repeat=repeat,after_render=render,after_status=status,final=snap(),manifest_bound=manifest.bound,run_id=report['run_id'],stateDir=report['stateDir'],missing_state_absent=True),indent=2))
origins={}
for n in ['operator_console','operator_console.sources','keeperhub_executor','keeperhub_executor.authorization','hosted_sepolia','moonwell_demo','wayfinder_paths','wayfinder_paths.core.utils.executor']:
 q=Path(importlib.import_module(n).__file__).resolve();assert q.is_relative_to(C) if not n.startswith('wayfinder_paths') else q.is_relative_to(Path(__import__('wayfinder_paths').__file__).resolve().parent);origins[n]=dict(path=str(q),sha256=hashlib.sha256(q.read_bytes()).hexdigest())
(O/'ORIGINS.json').write_text(json.dumps(origins,indent=2));print('synthetic user path passed')
