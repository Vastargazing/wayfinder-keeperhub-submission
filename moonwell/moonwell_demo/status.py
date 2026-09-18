"""Describe an existing run without becoming one of its writers.

``run_loop.py status`` answers from files that already exist and creates none of
them. That has to be arranged deliberately: ``RunPlan``'s constructor makes the
directory, takes an exclusive lock and writes a fresh checkpoint when it finds
none, and ``OperationJournal``'s constructor creates the database. Neither may be
used to answer a question about a run — least of all about a run that is not
there, where they would manufacture a plausible empty one.

Two boundaries this module states rather than hides:

* The checkpoint and the journal are two separate reads. A writer may move
  between them, so the pair can disagree. The checkpoint's data and the digest
  that identifies them come from one read; the file is read again after the
  journal and any difference is a refusal, never a mixture of two versions. A
  shared run id is not by itself agreement either: the operations, their step
  bindings and the checkpoint's own finished calls have to describe the same
  run, and where they do not, this says so instead of reporting success.
* SQLite's ``mode=ro`` refuses writes to the database. It does not promise an
  unchanged directory: a read may still create or update the ``-wal``/``-shm``
  sidecars. This module never claims otherwise.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .plan import CheckpointUnreadable, load_checkpoint

CHECKPOINT_NAME = 'run-plan.json'
JOURNAL_NAME = 'sdk-journal.sqlite'
OPERATION_COLUMNS = ('operation_id', 'sender', 'chain_id', 'digest', 'envelope', 'state',
                     'txn_hash', 'consumed', 'created_at', 'updated_at')


class StatusUnavailable(Exception):
    """This run cannot be described from what exists. Nothing was created or changed."""


@dataclass(frozen=True)
class RunState:
    """One reading of an existing run: its identity, its checkpoint and its journal."""

    state_dir: Path
    run_id: str
    checkpoint: dict
    rows: list[dict]
    tables: tuple[str, ...]
    step_bindings: dict[str, str]
    checkpoint_sha256: str


def read_journal(path: Path) -> dict:
    """One read-only snapshot of an existing SDK journal, in one transaction.

    The database is opened through a ``mode=ro`` URI, so SQLite itself refuses a
    write and refuses to create the file. Rows come back in the shape
    ``OperationJournal.entries()`` returns, so callers do not need the writer.
    """
    path = Path(path)
    if not path.is_file():
        raise StatusUnavailable(f'no SDK journal at {path}')
    try:
        conn = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)
    except sqlite3.Error as exc:
        raise StatusUnavailable(f'{path} cannot be opened read-only: {exc}') from None
    try:
        conn.execute('PRAGMA query_only = ON')
        conn.execute('BEGIN')
        tables = tuple(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
        if 'operations' not in tables:
            raise StatusUnavailable(
                f'{path} is a SQLite file without an `operations` table, so it is not an SDK journal')
        rows = []
        for row in conn.execute(
                f"SELECT {', '.join(OPERATION_COLUMNS)} FROM operations ORDER BY created_at"):
            item = dict(zip(OPERATION_COLUMNS, row, strict=True))
            item['envelope'] = json.loads(item['envelope'])
            item['consumed'] = bool(item['consumed'])
            rows.append(item)
        bindings = {}
        if 'execution_steps' in tables:
            bindings = {r[0]: r[1] for r in
                        conn.execute('SELECT step_id, operation_id FROM execution_steps')}
        driver_run_id = None
        if 'moonwell_run' in tables:
            found = conn.execute('SELECT run_id FROM moonwell_run WHERE singleton=1').fetchone()
            driver_run_id = found[0] if found else None
        conn.execute('ROLLBACK')
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise StatusUnavailable(f'{path} could not be read as an SDK journal: {exc}') from None
    finally:
        conn.close()
    return {'rows': rows, 'tables': tables, 'step_bindings': bindings,
            'driver_run_id': driver_run_id}


def incompatible(checkpoint, rows, bindings) -> list[str]:
    """Why this checkpoint and this journal are not one consistent run.

    The same run id on both sides only says they were started together. It does
    not say the journal still holds the operations the checkpoint's calls were
    bound to, nor that a call the driver recorded as finished has the submitted
    operation and hash behind it. Nothing here repairs or fills in anything: the
    answer is a list of reasons, and any reason at all is a refusal.

    The operator console keeps its own copy of these rules for artifacts it did
    not write. This is the driver's own statement of them, deliberately not
    imported from the console: production code must not depend on the operator UI
    to describe its own run.
    """
    problems = []
    operations = {row['operation_id']: row for row in rows}
    bound = set(bindings.values())
    calls = checkpoint['calls']
    for step_id, operation_id in sorted(bindings.items()):
        if operation_id not in operations:
            problems.append(f'step {step_id} is bound to operation {operation_id}, '
                            'which the journal does not hold')
        if step_id.removesuffix('/send/0') not in calls:
            problems.append(f'step {step_id} has no matching call in the checkpoint')
    for row in rows:
        if row['operation_id'] not in bound:
            problems.append(f"operation {row['operation_id']} has no driver step binding")
    for key, call in sorted(calls.items()):
        if not call.get('money') or call.get('state') != 'done':
            continue
        if call.get('composite'):
            children = [child for child, row in calls.items()
                        if child.startswith(key + '/') and row.get('money')
                        and not row.get('composite')]
            if not children or any(calls[child].get('state') != 'done' for child in children):
                problems.append(f'completed composite call {key} has no completed monetary child')
            continue
        operation = operations.get(bindings.get(key + '/send/0'))
        if operation is None or operation['state'] != 'submitted' or not operation['txn_hash']:
            problems.append(f'completed call {key} has no submitted journal operation')
            continue
        result = call.get('result')
        if isinstance(result, list) and len(result) == 2:
            result = result[1] if result[0] is True else None
        if isinstance(result, dict):
            result = result.get('transactionHash')
        if not isinstance(result, str) or result.lower() != operation['txn_hash'].lower():
            problems.append(f'completed call {key} records a result its journal operation '
                            'does not confirm')
    return problems


def read_state(state_dir) -> RunState:
    """Read an existing run's checkpoint and journal, or refuse to describe it."""
    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        raise StatusUnavailable(f'no run state directory at {state_dir}')
    checkpoint_path = state_dir / CHECKPOINT_NAME
    journal_path = state_dir / JOURNAL_NAME
    try:
        # One read: the data and the digest that identifies them come together.
        checkpoint, digest = load_checkpoint(checkpoint_path)
    except CheckpointUnreadable as exc:
        raise StatusUnavailable(str(exc)) from None

    journal = read_journal(journal_path)

    # The two files were read one after the other, not together. If the
    # checkpoint moved in between, a writer is working and this reading is of no
    # single moment: say so instead of publishing the mixture.
    try:
        again = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise StatusUnavailable(
            f'{checkpoint_path} could not be read again after the journal: {exc}') from None
    if again != digest:
        raise StatusUnavailable(
            f'{checkpoint_path} changed while the journal was being read; the run has an '
            'active writer and these two files are not one snapshot')

    run_id = checkpoint['run_id']
    driver_run_id = journal['driver_run_id']
    if driver_run_id is None:
        raise StatusUnavailable(
            f'{journal_path} holds no driver run identity, so nothing binds it to '
            f'{checkpoint_path}; this is not a described run')
    if driver_run_id != run_id:
        raise StatusUnavailable(
            f'{checkpoint_path} names run {run_id} and {journal_path} names {driver_run_id}; '
            'refusing to describe two different runs as one')
    problems = incompatible(checkpoint, journal['rows'], journal['step_bindings'])
    if problems:
        raise StatusUnavailable(
            f'{checkpoint_path} and {journal_path} name run {run_id} but do not describe one '
            'run: ' + '; '.join(problems))
    return RunState(state_dir=state_dir, run_id=run_id, checkpoint=checkpoint,
                    rows=journal['rows'], tables=journal['tables'],
                    step_bindings=journal['step_bindings'], checkpoint_sha256=digest)


def source_paths(state_dir) -> list[Path]:
    """Everything inside the state directory: all of it belongs to the run, not to us."""
    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        return []
    return [p for p in state_dir.rglob('*') if p.is_file() or p.is_symlink()]


def resolve_output(out, state_dir) -> Path:
    """The one file this command may write, resolved, or a refusal before writing.

    The rule is deliberately simple: a report about a run never lands inside that
    run's state directory, and never on a file of it reached by another name.

    What is checked and what is written are the same path — the resolved one,
    which is also the directory the saved ``stateDir`` is relative to. A path
    whose final component is a symlink is refused rather than followed: the
    report would then live somewhere other than where the operator named it, and
    the reader of a manifest resolves its ``stateDir`` from the directory of the
    file it was handed. Symlinked *parent* directories are fine, because the
    producer and the reader resolve them the same way.

    It is a rule about the path given, checked once, before anything is written.
    It is not a sandbox: it does not defend against the filesystem being
    rearranged underneath an open file.
    """
    out = Path(out)
    state_dir = Path(state_dir)
    if out.is_symlink():
        raise StatusUnavailable(
            f'--out {out} is a symlink. A report is read back from the path it was written '
            'to, and its saved stateDir is relative to that path\'s own directory, so a name '
            'standing for a file elsewhere is ambiguous: name the file itself.')
    resolved = out.resolve()
    state_resolved = state_dir.resolve()
    if resolved == state_resolved or resolved.is_relative_to(state_resolved):
        raise StatusUnavailable(
            f'--out {out} is inside the run state directory {state_dir}. A status report '
            'does not go into the state it describes; choose a path outside it.')
    try:
        target = resolved.stat()
    except OSError:
        return resolved                              # nothing there yet: nothing to protect
    for source in source_paths(state_dir):
        try:
            existing = source.stat()
        except OSError:
            continue
        if (existing.st_dev, existing.st_ino) == (target.st_dev, target.st_ino):
            raise StatusUnavailable(
                f'--out {out} is the same file as {source}, reached by another name. '
                'A status report never overwrites the run state it describes.')
    return resolved


def manifest_state_dir(state_dir, out) -> str:
    """Where the report says its run state is, as the reader of the report will look.

    The console resolves a relative ``stateDir`` against the directory holding
    the manifest, so that a state directory and its report can be moved together.
    This is computed from the two normalised paths, never from the literal
    command-line argument, which is relative to whatever directory the producer
    happened to run in.
    """
    return os.path.relpath(Path(state_dir).resolve(), Path(out).resolve().parent)


def unavailable_report(scenario, state_dir, out, reason) -> dict:
    """What a refusal writes down, if it writes anything at all.

    It carries no ``run_id``: a report that could not read the run must not look
    like a manifest that binds to one. The console refuses it for exactly that
    reason, which is the correct outcome.
    """
    return {'scenario': scenario, 'status': 'unavailable', 'reason': str(reason),
            'stateDir': manifest_state_dir(state_dir, out),
            'note': 'No run was described. This file is a refusal, not a run manifest: '
                    'it saves no run_id and must not be read as one.'}
