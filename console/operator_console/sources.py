"""Read-only readers for a run's artifacts.

Three artifacts, none of which this process may modify:

- ``sdk-journal.sqlite``  the SDK's ``OperationJournal``. Opened with SQLite's
  ``mode=ro`` URI plus ``PRAGMA query_only``, so a write is refused by SQLite
  itself rather than by our own care. The file is never created: a missing
  journal is reported, not conjured.
- ``run-plan.json``       the Moonwell driver checkpoint. Read as bytes and its
  recorded digest re-computed here; ``RunPlan`` itself is deliberately not
  imported, because opening one takes an exclusive lock and writes a lock file.
- a run manifest         any JSON a run emitted about itself (for example
  ``moonwell/scripts/run_loop.py --scenario status``). It is the only place
  mode labels may come from, and it is accepted only when it names this run's
  own state directory.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JOURNAL_NAME = "sdk-journal.sqlite"
CHECKPOINT_NAME = "run-plan.json"
FIXTURE_MARKER = "FIXTURE.txt"

OPERATION_COLUMNS = (
    "operation_id",
    "sender",
    "chain_id",
    "digest",
    "envelope",
    "state",
    "txn_hash",
    "consumed",
    "created_at",
    "updated_at",
)


class SourceUnavailable(Exception):
    """A required artifact is absent or unreadable, stated plainly."""


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class JournalSnapshot:
    """One SQLite read transaction; sha256 identifies its logical projection."""

    path: Path
    sha256: str
    read_at: float
    operations: list[dict[str, Any]]
    step_bindings: dict[str, str]
    driver_run_id: str | None
    tables: tuple[str, ...]

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        return next((r for r in self.operations if r["operation_id"] == operation_id), None)

    def operation_for_step(self, step_id: str) -> dict[str, Any] | None:
        bound = self.step_bindings.get(step_id)
        return self.operation(bound) if bound else None

    def step_for_operation(self, operation_id: str) -> str | None:
        return next((s for s, op in self.step_bindings.items() if op == operation_id), None)


def read_journal(path: Path) -> JournalSnapshot:
    """Open the journal read-only and take one consistent snapshot of it."""
    path = Path(path)
    if not path.is_file():
        raise SourceUnavailable(f"no SDK journal at {path}")
    read_at = time.time()
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        # mode=ro already refuses writes; query_only states the intent in-band.
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        tables = tuple(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        )
        if "operations" not in tables:
            raise SourceUnavailable(
                f"{path} is a SQLite file but has no `operations` table, so it is not an SDK journal"
            )
        operations = []
        # created_at has millisecond resolution, so two operations begun inside
        # one millisecond would otherwise tie. rowid breaks the tie by insertion
        # order, which is the order the SDK actually allocated them in.
        for index, row in enumerate(conn.execute(
            f"SELECT {', '.join(OPERATION_COLUMNS)} FROM operations"
            " ORDER BY created_at, rowid"
        )):
            item = dict(row)
            item["envelope"] = json.loads(item["envelope"])
            if not isinstance(item["envelope"], dict) or item["consumed"] not in (0, 1):
                raise ValueError("invalid operation envelope/consumed shape")
            item["consumed"] = bool(item["consumed"])
            item["seq"] = index
            operations.append(item)
        bindings: dict[str, str] = {}
        if "execution_steps" in tables:
            bindings = {
                r[0]: r[1]
                for r in conn.execute("SELECT step_id, operation_id FROM execution_steps")
            }
        driver_run_id = None
        if "moonwell_run" in tables:
            row = conn.execute("SELECT run_id FROM moonwell_run WHERE singleton=1").fetchone()
            driver_run_id = row[0] if row else None
        digest = hashlib.sha256(json.dumps({
            "format": "operator-console-journal-v1",
            "tables": tables,
            "operations": operations,
            "step_bindings": bindings,
            "driver_run_id": driver_run_id,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False).encode("utf-8")).hexdigest()
        conn.execute("ROLLBACK")  # End the read transaction; no database mutation.
    finally:
        # close also rolls back on every error path.
        conn.close()
    return JournalSnapshot(
        path=path,
        sha256=digest,
        read_at=read_at,
        operations=operations,
        step_bindings=bindings,
        driver_run_id=driver_run_id,
        tables=tables,
    )


@dataclass(frozen=True)
class CheckpointSnapshot:
    """The driver checkpoint plus whether its own recorded digest still holds."""

    path: Path
    sha256: str
    data: dict[str, Any]
    integrity: str  # "verified" | "mismatch" | "malformed"

    @property
    def run_id(self) -> str | None:
        value = self.data.get("run_id")
        return value if isinstance(value, str) else None

    @property
    def strategy(self) -> str | None:
        value = self.data.get("strategy")
        return value if isinstance(value, str) else None

    @property
    def iterations(self) -> list[dict[str, Any]]:
        value = self.data.get("iterations")
        return list(value) if isinstance(value, list) else []

    @property
    def calls(self) -> dict[str, Any]:
        value = self.data.get("calls")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def in_flight(self) -> dict[str, Any] | None:
        return next((r for r in self.iterations if r.get("status") != "done"), None)


def read_checkpoint(path: Path) -> CheckpointSnapshot:
    """Read the checkpoint without taking RunPlan's writer lock."""
    path = Path(path)
    if not path.is_file():
        raise SourceUnavailable(f"no driver checkpoint at {path}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        stored = json.loads(raw)
        data = stored["data"]
        recorded = stored["sha256"]
    except Exception as exc:  # noqa: BLE001 - any shape problem is one report
        return CheckpointSnapshot(path, digest, {}, f"malformed: {exc}")
    computed = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    integrity = "verified" if computed == recorded else "mismatch"
    if integrity == "verified":
        try:
            _validate_checkpoint(data)
        except (ValueError, TypeError, AttributeError) as exc:
            integrity = f"malformed: {exc}"
    return CheckpointSnapshot(path, digest, data if isinstance(data, dict) else {}, integrity)


@dataclass(frozen=True)
class ManifestSnapshot:
    """A run's own description of itself, and whether it describes THIS run.

    Mode labels are the reason this exists. They are never carried across runs,
    so a manifest is only allowed to label the state directory it names.
    """

    path: Path
    sha256: str
    data: dict[str, Any]
    bound: bool
    binding_detail: str

    @property
    def mode_labels(self) -> dict[str, Any]:
        value = self.data.get("mode_labels") if self.bound else None
        return dict(value) if isinstance(value, dict) else {}

    @property
    def not_proven(self) -> list[str]:
        value = self.data.get("not_proven") if self.bound else None
        return [str(v) for v in value] if isinstance(value, list) else []

    @property
    def corroboration(self) -> dict[str, list[dict[str, Any]]]:
        """Chain observations the run attached to named steps.

        Displayed as corroboration and never consulted when deciding whether a
        step executed. Shape: ``{"<call key>": [{"label", "value", "source"}]}``.
        """
        value = self.data.get("corroboration") if self.bound else None
        if not isinstance(value, dict):
            return {}
        out: dict[str, list[dict[str, Any]]] = {}
        for key, entries in value.items():
            if isinstance(entries, list):
                out[str(key)] = [dict(e) for e in entries if isinstance(e, dict)]
        return out

    @property
    def position(self) -> dict[str, Any]:
        value = self.data.get("position") if self.bound else None
        return dict(value) if isinstance(value, dict) else {}


def read_manifest(
    path: Path, state_dir: Path, *, journal: JournalSnapshot | None = None,
    checkpoint: CheckpointSnapshot | None = None,
) -> ManifestSnapshot:
    """Resolve stateDir relative to the manifest file, then verify saved identity.

    No identity is inferred or inserted into the manifest. A movable bundle
    saves a relative stateDir and run_id when it is created.
    """
    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        return ManifestSnapshot(path, digest, {}, False, f"unreadable JSON: {exc}")
    if not isinstance(data, dict):
        return ManifestSnapshot(path, digest, {}, False, "manifest is not a JSON object")

    def refused(reason):
        return ManifestSnapshot(path, digest, data, False, reason)

    declared = data.get("stateDir")
    if not isinstance(declared, str) or not declared:
        return refused("the manifest names no stateDir")
    target = Path(declared)
    if not target.is_absolute():
        target = path.parent / target
    if target.resolve() != Path(state_dir).resolve():
        return refused(f"the manifest describes {declared}, not {state_dir}; "
                       "mode labels are never carried from one run to another")
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return refused("manifest has no saved run_id; directory names alone are insufficient")
    if journal is None or not journal.driver_run_id:
        return refused("journal has no available saved run identity")
    if checkpoint is not None and checkpoint.integrity != "verified":
        return refused("checkpoint integrity/structure is unverified; binding refused")
    identities = [journal.driver_run_id]
    if checkpoint is not None:
        identities.append(checkpoint.run_id)
    if any(identity != run_id for identity in identities):
        return refused("manifest/checkpoint/journal run_id contradiction; binding refused")
    if checkpoint is not None and checkpoint_problems(checkpoint, journal):
        return refused("checkpoint/journal evidence is incompatible; binding refused")
    return ManifestSnapshot(path, digest, data, True,
        "saved run_id agrees with available run sources; stateDir resolved relative "
        "to the manifest's directory. Manifest content is a declaration, not signer/chain proof.")


def _validate_checkpoint(d: Any) -> None:
    """Read-side v2 shape checks, without RunPlan's locking/writing constructor."""
    if (not isinstance(d, dict) or d.get("version") != 2
        or not isinstance(d.get("run_id"), str) or len(d["run_id"]) != 32
        or d.get("strategy") != "moonwell-wsteth-loop"
        or not isinstance(d.get("iterations"), list) or not isinstance(d.get("calls"), dict)
        or "seed" not in d):
        raise ValueError("missing run/strategy/version/iterations/calls/seed")
    seed = d["seed"]
    if seed is not None and (not isinstance(seed, dict)
        or seed.get("state") not in {"intent", "done", "external"} or "usdc_amount" not in seed):
        raise ValueError("invalid seed state")
    for i, row in enumerate(d["iterations"], 1):
        if (not isinstance(row, dict) or row.get("index") != i
            or type(row.get("borrow_amt_wei")) is not int or row["borrow_amt_wei"] <= 0
            or row.get("status") not in {"in_flight", "done", "stopped"}
            or (row["status"] == "done" and type(row.get("lend_amt_wei")) is not int)):
            raise ValueError("invalid iteration")
    for key, row in d["calls"].items():
        if (not key.startswith(d["run_id"] + "/") or not isinstance(row, dict)
            or row.get("state") not in {"started", "done"} or not isinstance(row.get("intent"), dict)
            or (row["state"] == "done" and "result" not in row)
            or any(flag in row and type(row[flag]) is not bool for flag in ("money", "composite"))):
            raise ValueError("incomplete call checkpoint")
        if key.endswith("/best_quote") and row.get("expires_at") is not None:
            expiry = row["expires_at"]
            if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
                raise ValueError("invalid quote expiry")


def checkpoint_problems(checkpoint: CheckpointSnapshot | None,
                        journal: JournalSnapshot) -> list[str]:
    """Detect incompatible artifacts; agreement is not cross-file atomicity."""
    problems = []
    if checkpoint is None:
        return ["No driver checkpoint was read; run progress is unverified."]
    if checkpoint.run_id and journal.driver_run_id and checkpoint.run_id != journal.driver_run_id:
        problems.append("the checkpoint and the journal name different runs; run_id contradiction")
    if checkpoint.integrity != "verified":
        problems.append("The driver checkpoint's own digest does not check out or its structure "
                        f"is invalid ({checkpoint.integrity}); driver progress is unverified.")
        return problems
    if not journal.driver_run_id:
        problems.append("journal has no driver run_id binding; checkpoint ownership is unverified")
    for step_id, operation_id in journal.step_bindings.items():
        if journal.operation(operation_id) is None:
            problems.append(f"journal binding {step_id} names an absent operation")
        key = step_id.removesuffix("/send/0")
        if key not in checkpoint.calls:
            problems.append(f"journal binding {step_id} has no matching checkpoint call")
    bound_operations = set(journal.step_bindings.values())
    for operation in journal.operations:
        if operation["operation_id"] not in bound_operations:
            problems.append(f"journal operation {operation['operation_id']} has no driver step binding")
    for key, row in checkpoint.calls.items():
        if row.get("money") and not row.get("composite") and row["state"] == "done":
            operation = journal.operation_for_step(key + "/send/0")
            if not operation or operation["state"] != "submitted" or not operation["txn_hash"]:
                problems.append(f"completed checkpoint call {key} lacks its submitted journal operation")
            else:
                result = row.get("result")
                if isinstance(result, list) and len(result) == 2:
                    result = result[1] if result[0] is True else None
                if isinstance(result, dict):
                    result = result.get("transactionHash")
                if not isinstance(result, str) or result.lower() != operation["txn_hash"].lower():
                    problems.append(f"completed checkpoint call {key} result contradicts its journal hash")
        if row.get("money") and row.get("composite") and row["state"] == "done":
            leaves = [child for child, r in checkpoint.calls.items() if child.startswith(key + "/")
                      and r.get("money") and not r.get("composite")]
            if not leaves or any(checkpoint.calls[child]["state"] != "done" for child in leaves):
                problems.append(f"completed composite call {key} lacks completed monetary children")
    return problems


@dataclass(frozen=True)
class StateDirectory:
    """What was found in a state directory, and what was looked for and missing."""

    path: Path
    journal: JournalSnapshot | None = None
    checkpoint: CheckpointSnapshot | None = None
    manifest: ManifestSnapshot | None = None
    fixture_notice: str = ""
    problems: list[str] = field(default_factory=list)
    probed: list[str] = field(default_factory=list)


def load_state_directory(state_dir: Path, manifest_path: Path | None = None) -> StateDirectory:
    """Load whatever is present. Absence is reported, never filled in."""
    state_dir = Path(state_dir)
    problems: list[str] = []
    probed: list[str] = []
    journal = checkpoint = manifest = None

    journal_path = state_dir / JOURNAL_NAME
    probed.append(str(journal_path))
    try:
        journal = read_journal(journal_path)
    except (SourceUnavailable, OSError, sqlite3.Error, ValueError, TypeError) as exc:
        problems.append(str(exc))

    checkpoint_path = state_dir / CHECKPOINT_NAME
    probed.append(str(checkpoint_path))
    try:
        checkpoint = read_checkpoint(checkpoint_path)
    except (SourceUnavailable, OSError, sqlite3.Error, ValueError, TypeError) as exc:
        problems.append(str(exc))

    if manifest_path is not None:
        probed.append(str(manifest_path))
        try:
            manifest = read_manifest(Path(manifest_path), state_dir, journal=journal, checkpoint=checkpoint)
        except (SourceUnavailable, OSError, sqlite3.Error, ValueError, TypeError) as exc:
            problems.append(str(exc))

    marker = state_dir / FIXTURE_MARKER
    notice = ""
    try:
        marker.lstat()  # Distinguish absence from an existing broken symlink.
    except FileNotFoundError:
        pass  # Absence is not evidence that the run is authentic.
    except OSError as exc:
        notice = "This directory may contain synthetic fixture data; its marker cannot be checked."
        problems.append(f"{marker}: fixture marker unavailable: {exc}")
    else:
        try:
            if not marker.is_file():
                raise OSError("fixture marker is not a readable regular file")
            notice = marker.read_text(encoding="utf-8").strip()
            if not notice:
                raise ValueError("fixture marker is empty")
        except (OSError, UnicodeError, ValueError) as exc:
            notice = "This directory may contain synthetic fixture data; its marker cannot be read."
            problems.append(f"{marker}: fixture marker unavailable: {exc}")

    return StateDirectory(
        path=state_dir,
        journal=journal,
        checkpoint=checkpoint,
        manifest=manifest,
        fixture_notice=notice,
        problems=problems,
        probed=probed,
    )


# State directory layouts the repository's own scripts use, probed when the
# caller names none. Both are gitignored runtime state, so an empty result is
# the expected answer in a clean checkout and is reported as such.
KNOWN_STATE_ROOTS = ("moonwell/state", "integration/state")


def probe_known_layouts(repo_root: Path) -> list[Path]:
    """Directories under the repository's own layouts that hold a journal."""
    found = []
    for rel in KNOWN_STATE_ROOTS:
        root = Path(repo_root) / rel
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if (child / JOURNAL_NAME).is_file():
                found.append(child)
    return found
