"""Bounded lifecycle for subprocess trees owned by the offline runners (POSIX).

Each command gets its own session. Nested runners forward TERM while unwinding
so their independently owned sessions are cleaned before the outer KILL deadline.
No caller/supervisor process group is signalled. Escaped sessions created by an
arbitrary uncooperative command are outside this ownership contract.
"""
import os
from pathlib import Path
import signal
import subprocess
import time


class _Interrupted(BaseException):
    def __init__(self, signum):
        self.signum = signum


def _members(group):
    """Live members of this owned group; zombies have already terminated."""
    members = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # comm may contain spaces or parentheses.
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == group and fields[0] != 'Z':
                members.append({'pid': int(entry.name), 'start': fields[19]})
        except (FileNotFoundError, ProcessLookupError):
            continue
    return members


def run_process(command, *, cwd, env, log, timeout):
    """Return exit/timeout/cleanup evidence, keeping raw output in ``log``.

    Timeout remains 124. Normal and nonzero exits also clean remaining children.
    TERM gets 3 seconds, then KILL gets 1 second. A nested runner interrupted by
    its parent uses 1 second for TERM, leaving time inside the parent's deadline.
    """
    if timeout <= 0:
        raise ValueError('timeout must be positive')
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True)
    group = process.pid
    assert group != os.getpgrp()
    signals = []
    interrupted = None
    timed_out = False
    previous = signal.getsignal(signal.SIGTERM)

    def forward(signum, frame):
        raise _Interrupted(signum)

    def send(signum):
        try:
            os.killpg(group, signum)
            signals.append(signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, forward)
    try:
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            code = 124
            log.write(f'\nTIMEOUT after {timeout}s; terminating owned process group {group}\n')
            log.flush()
        except _Interrupted as exc:
            interrupted = exc.signum
            code = 128 + exc.signum
        finally:
            # Repeated TERM must not interrupt cleanup of our own children.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            remaining = _members(group)
            if remaining:
                send(signal.SIGTERM)
                deadline = time.monotonic() + (1 if interrupted else 3)
                while _members(group) and time.monotonic() < deadline:
                    time.sleep(0.02)
                if _members(group):
                    send(signal.SIGKILL)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                send(signal.SIGKILL)
                process.wait(timeout=1)
            deadline = time.monotonic() + 1
            while _members(group) and time.monotonic() < deadline:
                time.sleep(0.02)
            remaining = _members(group)
    finally:
        signal.signal(signal.SIGTERM, previous)
    result = dict(exit=code, timeout=timeout, timed_out=timed_out,
                  seconds=time.monotonic()-started, pid=process.pid,
                  process_group=group, cleanup_signals=signals,
                  remaining_live_members=remaining)
    if remaining:
        raise RuntimeError(f'owned process group did not terminate: {result}')
    if interrupted:
        raise SystemExit(128 + interrupted)
    return result
