#!/usr/bin/env python3
"""Verify the exact distributed file inventory. Build outputs are reported separately."""
from pathlib import Path,PurePosixPath
import hashlib,json,sys,argparse
R=Path(__file__).resolve().parents[2]
a=json.loads((R/'SNAPSHOT-MANIFEST.json').read_text())
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--prepared-sdk',action='store_true',help='explicitly account for separately generated wayfinder/upstream and check its eight recipe targets')
args=parser.parse_args()
errors=[]
if args.prepared_sdk:
    sdk=R/'wayfinder/upstream'
    if not sdk.is_dir() or sdk.is_symlink() or (R/'wayfinder').is_symlink():errors.append(['wayfinder/upstream','physical prepared SDK required'])
    recipe=json.loads((R/'patches/reconstruction.json').read_text())['wayfinder']['files']
    for n,h in recipe.items():
        q=sdk/n
        if not q.is_file() or q.is_symlink() or hashlib.sha256(q.read_bytes()).hexdigest()!=h:errors.append(['wayfinder/upstream/'+n,'prepared recipe mismatch'])
for n,expected in a['files'].items():
    p=PurePosixPath(n)
    if p.is_absolute() or '..' in p.parts or any(x.startswith('.') for x in p.parts) and n!='.gitignore':errors.append([n,'unsafe name']);continue
    q=R/n
    if q.is_symlink() or not q.is_file():errors.append([n,'missing or symlink']);continue
    if hashlib.sha256(q.read_bytes()).hexdigest()!=expected:errors.append([n,'hash mismatch'])
actual={str(p.relative_to(R)) for p in R.rglob('*') if p.is_file() or p.is_symlink()}
extras=sorted(actual-set(a['files'])-{'SNAPSHOT-MANIFEST.json'})
# Editable packaging metadata may be created after unpacking. Never treat it as distributed source.
unsafe_extra=[n for n in extras if not any(x.endswith('.egg-info') for x in PurePosixPath(n).parts) and not (args.prepared_sdk and n.startswith('wayfinder/upstream/'))]
errors.extend([n,'unlisted file'] for n in unsafe_extra)
print(json.dumps(dict(files=len(a['files']),errors=errors,non_distributed_generated_files=extras,prepared_sdk_explicit=args.prepared_sdk),indent=2))
sys.exit(bool(errors))
