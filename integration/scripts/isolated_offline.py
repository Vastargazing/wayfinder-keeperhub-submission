#!/usr/bin/env python3
"""Linux Landlock read isolation for a disposable delivery checkout.

Allow reading the delivery, interpreter and system libraries. The original
workspace is outside this allowlist; no namespace/container or service is used.
"""
import argparse
import ctypes
import os
from pathlib import Path
import subprocess
import sys


def restrict(root):
    libc = ctypes.CDLL(None, use_errno=True)
    # Linux x86_64 Landlock ABI: handle READ_FILE and READ_DIR only.
    class Ruleset(ctypes.Structure):
        _fields_ = [('handled_access_fs', ctypes.c_uint64)]
    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int)]
    read_file, read_dir = 1 << 2, 1 << 3
    attr = Ruleset(read_file | read_dir)
    fd = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if fd < 0: raise OSError(ctypes.get_errno(), 'Landlock create_ruleset')
    paths = [root, Path(sys.base_prefix).resolve(), *map(Path, ['/usr','/lib','/lib64','/bin','/etc','/dev','/proc','/sys'])]
    if os.environ.get('NODE_BIN'):
        paths.append(Path(os.environ['NODE_BIN']).resolve())
    for path in paths:
        if not path.exists(): continue
        opened = os.open(path, os.O_PATH | os.O_CLOEXEC)
        rule = PathRule((read_file | read_dir) if path.is_dir() else read_file, opened)
        if libc.syscall(445, fd, 1, ctypes.byref(rule), 0):
            raise OSError(ctypes.get_errno(), 'Landlock add_rule')
        os.close(opened)
    if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, fd, 0):
        raise OSError(ctypes.get_errno(), 'Landlock restrict_self')
    os.close(fd)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root',type=Path,required=True)
    ap.add_argument('--deny-probe',type=Path,required=True)
    ap.add_argument('command',nargs=argparse.REMAINDER)
    args=ap.parse_args();root=args.root.resolve()
    # Establish that the probe really exists and is readable before isolation.
    args.deny_probe.read_bytes()
    restrict(root)
    try: args.deny_probe.read_bytes()
    except PermissionError: print('original workspace read denied by Landlock',flush=True)
    else: raise AssertionError('original workspace unexpectedly readable')
    env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME'}}
    temp=root/'.offline-tmp';temp.mkdir(exist_ok=True)
    env.update(TMPDIR=str(temp),PYTHONDONTWRITEBYTECODE='1',GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_NOSYSTEM='1')
    cmd=args.command[1:] if args.command[:1]==['--'] else args.command
    return subprocess.call(cmd,cwd=root,env=env)

if __name__=='__main__': sys.exit(main())
