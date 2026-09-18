#!/usr/bin/env python3
"""Archive the pinned SDK from available Git objects and apply the exact recipe; no fetch."""
import argparse,json
from pathlib import Path
from check_sdk_snapshot import SDK_BASE,SDK_PATCHES,ROOT,extract,run,verify_reconstruction
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--objects',type=Path,required=True);p.add_argument('--dest',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
a=p.parse_args();a.dest=a.dest.resolve();a.out=a.out.resolve()
if a.dest.exists():p.error('destination must not exist')
a.out.mkdir(parents=True,exist_ok=False)
extract(a.objects.resolve(),SDK_BASE,a.dest)
rows=[]
for name in SDK_PATCHES:
    rows.append(run(['git','apply','--check',str(ROOT/'patches'/name)],a.dest,a.out,name+'-check'))
    rows.append(run(['git','apply',str(ROOT/'patches'/name)],a.dest,a.out,name+'-apply'))
verify_reconstruction(a.dest,json.loads((ROOT/'patches/reconstruction.json').read_text()),'wayfinder',SDK_PATCHES,a.out/'reconstruction.json')
(a.out/'commands.json').write_text(json.dumps(rows,indent=2)+'\n')
