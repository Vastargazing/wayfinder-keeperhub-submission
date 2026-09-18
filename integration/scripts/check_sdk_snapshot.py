#!/usr/bin/env python3
"""SDK-only archive, exact patch recipe and behavioral checks. Does not replace combined server-source validation."""
import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[2]
SDK_BASE = '46dbf7c05e7f17e6c10a136da0dae6e13e590e95'
SDK_PATCHES = ['wayfinder-external-executor-seam.patch','wayfinder-base-sepolia.patch','wayfinder-durable-execution.patch']


def verify_reconstruction(tree, manifest, source, patches, output):
    """Read-only verification: expected metadata is never generated here."""
    entry=manifest[source]
    pin=SDK_BASE
    targets=set()
    for name in patches:
        for line in (ROOT/'patches'/name).read_text().splitlines():
            if line.startswith('+++ b/'): targets.add(line[6:])
    actual={rel:hashlib.sha256((tree/rel).read_bytes()).hexdigest() for rel in sorted(targets)}
    expected=entry['files']
    result=dict(source=source,base=pin,actual=actual,expected=expected,
                matches=entry['base']==pin and actual==expected)
    output.write_text(json.dumps(result,indent=2)+'\n')
    if not result['matches']:
        raise ValueError(f'{source} reconstruction differs from expected metadata; see {output}')


def extract(repo, pin, dest):
    dest.mkdir(parents=True)
    with tempfile.TemporaryFile() as stream:
        subprocess.run(['git','-C',str(repo),'archive',pin],stdout=stream,check=True)
        stream.seek(0)
        with tarfile.open(fileobj=stream) as archive: archive.extractall(dest,filter='data')


def run(cmd, cwd, out, name, env=None, expect=0):
    p=subprocess.run(cmd,cwd=cwd,env=env,text=True,capture_output=True)
    (out/(name+'.log')).write_text(p.stdout+p.stderr)
    assert p.returncode==expect,(name,p.returncode,p.stdout[-1000:],p.stderr[-1000:])
    return dict(name=name,command=cmd,exit=p.returncode,log=name+'.log')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--objects',type=Path,required=True)
    ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args()
    from offline_checks import module_roots, observed_command
    args.out=args.out.resolve()
    args.objects=args.objects.resolve()
    args.out.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((ROOT/'patches/reconstruction.json').read_text())
    results=[]
    with tempfile.TemporaryDirectory(prefix='upstream-check-') as td:
        temp=Path(td);sdk=temp/'sdk'
        extract(args.objects,SDK_BASE,sdk)
        for name in SDK_PATCHES:
            results.append(run(['git','apply','--check',str(ROOT/'patches'/name)],sdk,args.out,name+'-check'))
            results.append(run(['git','apply',str(ROOT/'patches'/name)],sdk,args.out,name+'-apply'))
        verify_reconstruction(sdk,manifest,'wayfinder',SDK_PATCHES,args.out/'sdk-reconstruction.json')
        env={**os.environ,'PYTHONPATH':os.pathsep.join(map(str,[sdk,ROOT/'integration',ROOT/'hosted-sepolia']))}
        results.append(run([sys.executable,'-c',
            'import pathlib,wayfinder_paths; p=pathlib.Path(wayfinder_paths.__file__).resolve(); print(p); assert p.is_relative_to(pathlib.Path.cwd())'],
            sdk,args.out,'sdk-import-origin',env))
        results.append(run(observed_command(['-m','pytest','wayfinder_paths/core/utils/test_executor.py',
            'wayfinder_paths/adapters/aave_v3_adapter/test_adapter.py','-o','addopts=','-q',
            '--junitxml='+str(args.out/'sdk-junit.xml')],args.out/'sdk-origins.json',module_roots(sdk)),sdk,args.out,'sdk-tests',env))
        # Real lookup/journal behavior plus the accepted hosted REC-1 controls.
        results.append(run(observed_command(['-m','pytest',str(ROOT/'integration/tests/test_sdk_contract.py'),
            str(ROOT/'hosted-sepolia/tests/test_rec1.py'),
            str(ROOT/'hosted-sepolia/tests/test_rejected_observations.py'),
            '-q','--junitxml='+str(args.out/'contract-junit.xml')],
            args.out/'contract-origins.json',module_roots(sdk)),ROOT,args.out,'sdk-contract',env))
    (args.out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2))

if __name__=='__main__': main()
