#!/usr/bin/env python3
"""Verify the exact distributed file inventory. Build outputs are reported separately."""
from pathlib import Path,PurePosixPath
import hashlib,json,os,sys,argparse
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
def inventory(root):
    """Every file and symlink below the physical root, never following symlinks.
    The only path skipped is the root's own `.git` DIRECTORY: a normal Git clone's checkout metadata,
    which is not distributed content. It is recognised by name and type before any descent, and nothing
    inside it is read or trusted. A root `.git` that is a symlink or a file (worktree/submodule layout)
    is refused; `.git` directories anywhere else are ordinary unlisted files. This is a single pass over
    the tree as found, not a guard against a filesystem rearranged while it runs."""
    found=set();skipped=None;stack=[root]
    while stack:
        d=stack.pop()
        with os.scandir(d) as entries:
            for e in entries:
                rel=os.path.relpath(e.path,root)
                if d==root and e.name=='.git':
                    if e.is_symlink():errors.append(['.git','root .git is a symlink; not accepted as clone metadata']);continue
                    if e.is_dir(follow_symlinks=False):skipped=rel;continue
                    errors.append(['.git','root .git is not a directory; worktree or .git-file layouts are not supported, use a normal clone or git archive']);continue
                if e.is_dir(follow_symlinks=False):stack.append(e.path)
                else:found.add(rel)
    return found,skipped
actual,root_git=inventory(str(R))
extras=sorted(actual-set(a['files'])-{'SNAPSHOT-MANIFEST.json'})
# Editable packaging metadata may be created after unpacking. Never treat it as distributed source.
unsafe_extra=[n for n in extras if not any(x.endswith('.egg-info') for x in PurePosixPath(n).parts) and not (args.prepared_sdk and n.startswith('wayfinder/upstream/'))]
errors.extend([n,'unlisted file'] for n in unsafe_extra)
print(json.dumps(dict(files=len(a['files']),errors=errors,non_distributed_generated_files=extras,prepared_sdk_explicit=args.prepared_sdk,root_git_directory_skipped=root_git is not None),indent=2))
sys.exit(bool(errors))
