"""Versioned, single-writer driver intent and replay checkpoints.

A completed call records its result separately from the SDK's consumed bit.
Incomplete borrow/wrap calls may re-enter the real adapter with a named SDK step;
swap/lend/collateral continue through checkpointed named children. Legacy
incomplete calls and unresolved operations stop for operator reconciliation.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace


class CheckpointError(BaseException):
    """Safety stop that upstream retry/rollback handlers must not swallow."""


CHECKPOINT_VERSION = 2


class CheckpointUnreadable(Exception):
    """An existing checkpoint cannot be read as a v2 checkpoint; nothing was written.

    Deliberately an ordinary ``Exception``, unlike :class:`CheckpointError`: a
    reader saying it cannot describe a run is not the writer's safety stop, and
    a handler that reports it must not therefore swallow the safety stop too.
    """


def checkpoint_digest(data):
    """The digest stored beside the data. One definition, writer and reader alike."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _named(value, allowed):
    """Is this value one of ``allowed``? A value of the wrong type is not.

    Set membership asks the value for a hash, and JSON supplies lists and
    objects, which have none. Asking first would raise ``TypeError`` out of a
    validator that promises ``ValueError``, so the type is settled before the
    question: a checkpoint whose enum field is a list is invalid, not
    unanswerable.
    """
    return isinstance(value, str) and value in allowed


def validate_checkpoint(data):
    """Structure rules for a v2 checkpoint. No I/O, no lock, no repair.

    Raises ``ValueError``. This is the driver's own rule; the console has its own
    read-side copy for artifacts it did not write, and neither is relaxed for the
    other.
    """
    d = data
    if (not isinstance(d, dict) or d.get('version') != CHECKPOINT_VERSION
        or not isinstance(d.get('run_id'), str) or len(d['run_id']) != 32
        or d.get('strategy') != 'moonwell-wsteth-loop'
        or not isinstance(d.get('iterations'), list) or not isinstance(d.get('calls'), dict)
        or 'seed' not in d):
        raise ValueError('missing run/strategy/version/iterations/calls/seed')
    if d['seed'] is not None and (not isinstance(d['seed'], dict)
            or not _named(d['seed'].get('state'), {'intent', 'done', 'external'})
            or 'usdc_amount' not in d['seed']):
        raise ValueError('invalid seed state')
    for i, r in enumerate(d['iterations'], 1):
        # The type is checked before anything is asked of the value: an entry of
        # the wrong shape is an invalid checkpoint, not an AttributeError.
        if (not isinstance(r, dict) or r.get('index') != i
            or type(r.get('borrow_amt_wei')) is not int
            or r['borrow_amt_wei'] <= 0
            or not _named(r.get('status'), {'in_flight', 'done', 'stopped'})
            or (r['status'] == 'done' and type(r.get('lend_amt_wei')) is not int)):
            raise ValueError('invalid iteration')
    for k, r in d['calls'].items():
        if (not isinstance(k, str) or not isinstance(r, dict)
            or not k.startswith(d['run_id'] + '/')
            or not _named(r.get('state'), {'started', 'done'})
            or 'intent' not in r or (r['state'] == 'done' and 'result' not in r)):
            raise ValueError('incomplete call checkpoint')


def load_checkpoint(path):
    """Return ``(data, sha256)`` for an existing checkpoint, or refuse.

    This is the read half of ``RunPlan.__init__`` without its writer half: no
    directory is made, no lock file is opened, no checkpoint is written, and a
    legacy or damaged file is reported rather than migrated.

    The digest is of the very bytes these data were parsed from, returned
    together with them. A caller that hashes the file again afterwards would be
    describing whatever is there by then, which is not necessarily what it read.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CheckpointUnreadable(f'no readable checkpoint at {path}: {exc}') from None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        stored = json.loads(raw)
        data = stored['data']
        recorded = stored['sha256']
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise CheckpointUnreadable(
            f'{path} is not a checkpoint file: {type(exc).__name__}: {exc}') from None
    if recorded != checkpoint_digest(data):
        raise CheckpointUnreadable(f'{path}: checkpoint checksum mismatch')
    try:
        validate_checkpoint(data)
    except ValueError as exc:
        raise CheckpointUnreadable(
            f'{path}: invalid or legacy checkpoint; no automatic migration: {exc}') from None
    return data, digest


class RunPlan:
    VERSION = CHECKPOINT_VERSION

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.created = not self.path.exists()
        self._lock = self.path.with_suffix('.lock').open('a')
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise CheckpointError('run already has a writer') from None
        try:
            if self.path.exists():
                stored = json.loads(self.path.read_text())
                self.data = stored['data']
                if stored['sha256'] != self._digest():
                    raise ValueError('checkpoint checksum mismatch')
                self._validate()
            else:
                self.data = {'version': self.VERSION, 'run_id': uuid.uuid4().hex,
                             'strategy': 'moonwell-wsteth-loop', 'seed': None,
                             'iterations': [], 'calls': {}}
                self._flush()
        except BaseException as exc:
            self.close()
            raise CheckpointError(f'invalid or legacy checkpoint; no automatic migration: {exc}') from exc

    def _validate(self):
        validate_checkpoint(self.data)

    def close(self):
        if not self._lock.closed:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()

    def _digest(self):
        return checkpoint_digest(self.data)

    def _flush(self):
        tmp = self.path.with_suffix('.tmp')
        with tmp.open('w') as f:
            json.dump({'data': self.data, 'sha256': self._digest()}, f, sort_keys=True, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def record_seed(self, usdc_amount):
        if self.data['seed'] is not None:
            raise CheckpointError('seed already has an intent; do not replay initial deposit')
        self.data['seed'] = {'usdc_amount': usdc_amount, 'state': 'intent'}
        self._flush()

    def finish_seed(self, *, external=False):
        if self.data['seed'] is None:
            raise CheckpointError('seed intent missing')
        self.data['seed']['state'] = 'external' if external else 'done'
        self._flush()

    @property
    def seed_done(self):
        return self.data['seed'] is not None and self.data['seed']['state'] in {'done', 'external'}

    def start_iteration(self, borrow_amt_wei, borrowable_wei_before):
        if self.in_flight:
            raise CheckpointError('unfinished iteration; resume it before starting another')
        r = dict(index=len(self.data['iterations']) + 1, borrow_amt_wei=int(borrow_amt_wei),
                 borrowable_wei_before=borrowable_wei_before, status='in_flight', started_at=time.time())
        self.data['iterations'].append(r)
        self._flush()
        return SimpleNamespace(**r)

    def finish_iteration(self, lend_amt_wei, status):
        r = self.data['iterations'][-1]
        r.update(lend_amt_wei=lend_amt_wei, status='done' if status == 'done' else 'stopped')
        self._flush()

    @property
    def in_flight(self):
        return next((r for r in self.data['iterations'] if r['status'] != 'done'), None)

    @property
    def iterations(self):
        return list(self.data['iterations'])
