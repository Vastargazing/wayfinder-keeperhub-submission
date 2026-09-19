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
# Variables that bypass repository discovery or redirect object lookup; they are dropped for the SDK Git commands only.
EXPLICIT_GIT_ENV = ('GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_OBJECT_DIRECTORY','GIT_COMMON_DIR',
                    'GIT_ALTERNATE_OBJECT_DIRECTORIES','GIT_CEILING_DIRECTORIES','GIT_DISCOVERY_ACROSS_FILESYSTEM')


def sdk_git_env(tree=None, env=None):
    """Environment for the Git commands that build the SDK copy.

    `git apply` resolves a git-style diff against the top level of whatever repository it discovers from the
    current directory, and silently ignores hunks whose paths fall outside that directory. The documented layout
    puts the SDK copy inside the snapshot, so in a Git clone that discovery reaches the clone, the two git-style
    patches are read as repository-root-relative and apply nothing.

    The upward search is therefore stopped directly above `tree`: a ceiling entry must be a proper ancestor of the
    starting directory and is itself never entered, so naming the parent leaves exactly `tree` to be examined, and
    it has no repository of its own. The variables that would bypass discovery altogether or redirect object
    lookup are removed as well - for these commands only. The caller's own environment, the user's Git
    configuration and the surrounding repository are untouched, no repository is created inside the SDK copy, and
    this is not a general safety wrapper for other Git commands. A path containing ':' cannot be expressed in
    GIT_CEILING_DIRECTORIES, and a `tree` directly at the filesystem root has no parent to name; the discovery
    check in `apply_recipe` turns both into a refusal instead of a silently wrong reconstruction.
    """
    out={k:v for k,v in (os.environ if env is None else env).items() if k not in EXPLICIT_GIT_ENV}
    if tree is not None: out['GIT_CEILING_DIRECTORIES']=str(Path(tree).resolve().parent)
    return out


def apply_recipe(tree, out, patches=SDK_PATCHES):
    """Apply the pinned patches in order to `tree`, treated as a plain directory of files, and record every command.

    Both consumers - `prepare_sdk_snapshot.py` and this script's own reconstruction - go through here, so both
    rebuild the same pinned SDK from local objects and exactly these patches. No fetch, no download, no fallback
    to another SDK revision."""
    tree=Path(tree).resolve()
    env=sdk_git_env(tree)
    rows=[run(['git','rev-parse','--show-toplevel'],tree,out,'git-discovery',env,expect=None)]
    if rows[0]['exit']==0:
        raise ValueError(f'{tree} still resolves into a Git repository for these commands; the recipe would be '
                         f'applied against its root instead of this directory, see {out/"git-discovery.log"}')
    for name in patches:
        rows.append(run(['git','apply','--check',str(ROOT/'patches'/name)],tree,out,name+'-check',env))
        rows.append(run(['git','apply',str(ROOT/'patches'/name)],tree,out,name+'-apply',env))
    return rows


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
        subprocess.run(['git','-C',str(repo),'archive',pin],stdout=stream,check=True,env=sdk_git_env())
        stream.seek(0)
        with tarfile.open(fileobj=stream) as archive: archive.extractall(dest,filter='data')


def run(cmd, cwd, out, name, env=None, expect=0):
    """`expect=None` records the command's own exit instead of requiring one; the caller then decides."""
    p=subprocess.run(cmd,cwd=cwd,env=env,text=True,capture_output=True)
    (out/(name+'.log')).write_text(p.stdout+p.stderr)
    assert expect is None or p.returncode==expect,(name,p.returncode,p.stdout[-1000:],p.stderr[-1000:])
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
        results.extend(apply_recipe(sdk,args.out))
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
