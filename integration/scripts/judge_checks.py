#!/usr/bin/env python3
"""Snapshot local and SDK checks; excludes KeeperHub server-source validation."""
import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from process_control import run_process

ROOT=Path(__file__).resolve().parents[2]


def observed_command(arguments, output, roots):
    """Record every loaded project module, including nested SDK modules."""
    code = '''import hashlib,json,runpy,sys
from pathlib import Path
arguments,output,roots = json.loads(sys.argv[1])
sys.argv = arguments[1:] if arguments[0] == '-m' else arguments
try:
    if arguments[0] == '-m': runpy.run_module(arguments[1], run_name='__main__', alter_sys=True)
    else:
        sys.path.insert(0, str(Path(arguments[0]).resolve().parent))
        runpy.run_path(arguments[0], run_name='__main__')
finally:
    origins = {}
    violations = []
    for name,module in sorted(sys.modules.copy().items()):
        prefix = name.split('.')[0]
        if prefix not in roots: continue
        file = getattr(module, '__file__', None)
        if not file: continue
        path = Path(file).resolve()
        origins[name] = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        if not path.is_relative_to(Path(roots[prefix]).resolve()): violations.append(name)
    Path(output).write_text(json.dumps(dict(modules=origins, violations=violations), indent=2)+'\\n')
    if violations: raise RuntimeError('Project module origin mismatch: '+repr(violations))
'''
    return [sys.executable, '-c', code, json.dumps([arguments, str(output), {k:str(v) for k,v in roots.items()}])]


def module_roots(sdk):
    return dict(keeperhub_executor=ROOT/'integration/keeperhub_executor',
                hosted_sepolia=ROOT/'hosted-sepolia/hosted_sepolia',
                moonwell_demo=ROOT/'moonwell/moonwell_demo',
                operator_console=ROOT/'console/operator_console',
                wayfinder_paths=sdk/'wayfinder_paths')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--sdk',type=Path,default=Path(importlib.util.find_spec('wayfinder_paths').origin).resolve().parent.parent)
    ap.add_argument('--timeout', action='append', default=[], metavar='NAME=SECONDS',
                    help='explicit per-command budget; unspecified commands retain 240 seconds')
    ap.add_argument('--mutation-timeout', type=float, default=60,
                    help='budget for each mutation control/mutant process')
    ap.add_argument('--sdk-objects',type=Path,required=True)
    args=ap.parse_args()
    budgets = {}
    names = {'reconcile-help','integration','hosted','moonwell','console',
             'moonwell-gate','calldata','mutations','sdk-snapshot'}
    for item in args.timeout:
        try:
            name, value = item.split('=', 1)
            seconds = float(value)
            if name not in names or not 0 < seconds < float('inf'):
                raise ValueError()
        except ValueError:
            ap.error('--timeout requires a known NAME and finite positive SECONDS')
        budgets[name] = seconds
    if not 0 < args.mutation_timeout < float('inf'):
        ap.error('--mutation-timeout must be finite and positive')
    for option,marker in [('sdk','wayfinder_paths')]:
        path=getattr(args,option).resolve()
        if not (path/marker).is_dir():
            ap.error(f'{option} source tree not found at {path}; pass --{option} /path/to/{option} (must contain {marker})')
        setattr(args,option,path)
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    origins={}
    roots=module_roots(args.sdk)
    for name in roots:
        path=Path(importlib.import_module(name).__file__).resolve()
        assert path.is_relative_to(roots[name]),(name,str(path),'outside selected source')
        origins[name]=str(path)
    (out/'imports.json').write_text(json.dumps(origins,indent=2)+'\n')
    print(json.dumps(origins,indent=2),flush=True)
    commands=[
        ('reconcile-help',['hosted-sepolia/scripts/reconcile_hosted.py','--help']),
        ('integration',['-m','pytest','integration/tests','-q','--tb=short']),
        ('hosted',['-m','pytest','hosted-sepolia/tests','-q','--tb=short']),
        ('moonwell',['-m','pytest','moonwell/tests','-q','--tb=short']),
        ('console',['-m','pytest','console/tests','-q','--tb=short']),
        ('moonwell-gate',['moonwell/scripts/test_gate.py','--out',str(out/'moonwell-gate.json')]),
        ('calldata',['integration/scripts/calldata_selftest.py','--output',str(out/'calldata.json')]),
        ('mutations',['integration/scripts/offline_mutations.py','--sdk',str(args.sdk),'--out',str(out/'mutations'),'--timeout',str(args.mutation_timeout)]),
        ('sdk-snapshot',['integration/scripts/check_sdk_snapshot.py','--objects',str(args.sdk_objects),'--out',str(out/'sdk-snapshot')]),
    ]
    results=[]
    exit_code=0
    env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME','KEEPERHUB_API_KEY'}}
    # Package-local pytest configuration may override the root asyncio setting.
    # This also reaches the mutation runner's separate pytest processes.
    env['PYTEST_ADDOPTS']=(env.get('PYTEST_ADDOPTS','')+' --asyncio-mode=auto').strip()
    for name,args in commands:
        if args[:2] == ['-m','pytest']:
            args += ['--junitxml='+str(out/(name+'-junit.xml')), '--durations=20', '-v']
        command=observed_command(args,out/(name+'-origins.json'),roots)
        with (out/(name+'.log')).open('w') as log:
            outcome = run_process(command, cwd=ROOT, env=env, log=log,
                                  timeout=budgets.get(name, 240))
            code = outcome['exit']
        results.append(dict(name=name,command=command,log=name+'.log', **outcome))
        (out/'commands.json').write_text(json.dumps(results,indent=2)+'\n')
        print(f'{name}: exit {code}',flush=True)
        if code and not exit_code: exit_code=code
    return exit_code

if __name__=='__main__': sys.exit(main())
