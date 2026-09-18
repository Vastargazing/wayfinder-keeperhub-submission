"""Build a state directory that is unmistakably a fixture.

This repository ships no journal: a journal is runtime state, gitignored, and a
fabricated one presented as a run would be exactly the kind of claim the rest of
the project refuses to make. So the fixture is built on demand, marked on disk
with ``FIXTURE.txt``, and the screen shows that marker as a banner before it
shows anything else.

It is written with the real ``OperationJournal`` and ``RunPlan`` writers rather
than hand-rolled SQL, so a fixture cannot drift away from the shape the SDK and
the driver actually produce. Nothing here touches a chain, a signer or KeeperHub.

The builder writes only into a directory it created itself, because synthetic
data shaped like a run must never land on top of a real one. The final path has
to be new: an existing directory — empty, a finished fixture, the remains of a
failed build — an existing file, and a symlink of any kind are refused before
anything there is read, written or removed, and so is a path that reaches its
parent through a symlink or walks back up through ``..``. Missing parents are
created as needed; an I/O failure can leave some new parents in place. There is no force,
merge, reuse or resume flag; the answer to an occupied path is a different path.
"""

from __future__ import annotations

import json
from pathlib import Path

NOTICE = (
    "Synthetic data written by operator_console.fixture for demonstrating and "
    "testing this screen. There is no chain, no signer, no KeeperHub and no "
    "transaction behind any value on this page. Nothing was broadcast anywhere."
)

WALLET = "0x0000000000000000000000000000000000f1c700"
TOKEN = "0x0000000000000000000000000000000000000010"
POOL = "0x0000000000000000000000000000000000000020"
ROUTER = "0x0000000000000000000000000000000000000030"
CHAIN_ID = 8453

FIXTURE_MODE_LABELS = {
    "chain": "FIXTURE (no chain of any kind)",
    "signer": "FIXTURE (nothing signs; there is no key)",
    "keeperhub": "FIXTURE (no KeeperHub, hosted or self-hosted)",
    "sdk_key_material": "NONE (there is no SDK process behind this file)",
}


class FixtureTargetError(Exception):
    """The path could not be claimed as a new directory, so nothing was built.

    Raised instead of writing, so a caller that sees it knows the path is as it
    was found. The CLI turns it into a message and a non-zero exit; a library
    caller gets the exception rather than a quiet skip or a partial success.
    """


def build_fixture(directory: Path) -> Path:
    """Create a labelled fixture state directory and return its path.

    ``directory`` must not exist. Raise :class:`FixtureTargetError` if it does,
    or if the path leads through a symlink, having touched nothing. A failure
    while writing surfaces as itself; the incomplete directory is left for
    inspection and refused on the next attempt rather than silently reused.
    """
    # Imported before the directory is claimed, so a missing SDK leaves no
    # directory behind to be refused on the next try.
    from wayfinder_paths.core.utils.executor import (  # noqa: PLC0415
        OperationJournal,
        build_envelope,
    )

    directory = _claim_new_directory(directory)
    try:
        _write_fixture(directory, OperationJournal, build_envelope)
    except BaseException as failure:
        failure.add_note(
            f"The fixture in {directory} is incomplete: whatever is there is not a "
            "run and is not a usable fixture. It is left as it is rather than "
            "deleted, and building into it again is refused, so pass a new path."
        )
        raise
    return directory


def _claim_new_directory(directory: Path) -> Path:
    """Create this one directory, or refuse and leave the path untouched.

    ``mkdir`` without ``exist_ok`` is the whole mechanism: in one step the kernel
    either creates the final component or reports that something already holds
    the name, so two builders racing for the same free path cannot both go on.
    That single call is the entire atomicity claim, and it covers the final
    component only — not the chain of parents, which is created the ordinary
    ``mkdir -p`` way.

    Expected occupied-path and lexical-path refusals precede fixture writes.
    Other filesystem errors propagate and may leave newly created parents.
    ``..`` is refused because resolving a path that walks back up could create
    parents on the way to an already occupied target. The scan below reads the path
    as written to refuse a symlink standing where a directory should be, which
    ``mkdir`` would follow without complaint. None of this is protection against
    a filesystem rearranged while these calls run: this is a fixture builder,
    not a sandbox.
    """
    directory = Path(directory)
    absolute = directory if directory.is_absolute() else Path.cwd() / directory
    if '..' in absolute.parts:
        raise FixtureTargetError(
            f"{directory}: the path walks back up through '..'. Which directory "
            "that finally names depends on how the kernel resolves the rest of "
            "the path, and the missing parents would have to be created before "
            "finding out — so a refusal at the end would leave directories "
            "behind. Nothing was read or written. Name the directory directly."
        )
    for parent in absolute.parents:
        if parent.is_symlink():
            raise FixtureTargetError(
                f"{directory}: the path reaches {parent}, which is a symlink. A "
                "fixture is built only on a path of real directories, so that "
                "nothing is written through a link into somewhere else. Nothing "
                "was read or written. Name the directory to create directly."
            )
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise FixtureTargetError(
            f"{directory}: {_what_is_there(directory)} is already there, and a "
            "fixture is only ever built in a new directory. Nothing there was "
            "read, written or removed — an existing state directory would have "
            "had its checkpoint and run identity replaced and fixture operations "
            "mixed into its journal. What a failed build left behind is refused "
            "for the same reason. Pass a path that does not exist."
        ) from None
    except NotADirectoryError:
        raise FixtureTargetError(
            f"{directory}: a component of this path is not a directory, so there "
            "is nowhere to create it. Nothing was read or written."
        ) from None
    return directory


def _what_is_there(directory: Path) -> str:
    """Say what holds the name, for the refusal message and nothing else."""
    if directory.is_symlink():
        return "a symlink"
    return "a directory" if directory.is_dir() else "a file"


def _write_fixture(directory: Path, OperationJournal, build_envelope) -> None:
    """Fill a directory this module has just created, and only that directory."""
    # The marker goes down first: from here on the directory holds data shaped
    # like a run, and it must never be readable without the banner saying it is
    # not one.
    (directory / "FIXTURE.txt").write_text(NOTICE + "\n")

    run_id = "f" * 32
    plan = {
        "version": 2,
        "run_id": run_id,
        "strategy": "moonwell-wsteth-loop",
        "seed": {"usdc_amount": 0.0, "state": "external"},
        "iterations": [
            {"index": 1, "borrow_amt_wei": 10**17, "borrowable_wei_before": 10**20,
             "status": "done", "lend_amt_wei": 5 * 10**16, "started_at": 0.0},
            {"index": 2, "borrow_amt_wei": 8 * 10**16, "borrowable_wei_before": 9 * 10**19,
             "status": "in_flight", "started_at": 0.0},
        ],
        "calls": {},
    }

    journal = OperationJournal(directory / "sdk-journal.sqlite")
    try:
        root = f"{run_id}/iteration/2"
        steps = [
            (f"{root}/borrow/0", TOKEN, "0xc5ebeaec" + "1" * 64, "done", "landed"),
            (f"{root}/wrap_eth/0", POOL, "0xd0e30db0", "done", "landed"),
            (f"{root}/ensure-allowance/approve", TOKEN, "0x095ea7b3" + "2" * 64, "done", "landed"),
            (f"{root}/_swap_with_retries/0/swap_from_quote/transaction",
             ROUTER, "0xa9059cbb" + "3" * 64, "started", "pending"),
        ]
        for index, (key, to, data, call_state, journal_state) in enumerate(steps):
            envelope = build_envelope(
                {"chainId": CHAIN_ID, "from": WALLET, "to": to, "data": data, "value": 0}
            )
            row, _ = journal.bind_step(f"{key}/send/0", envelope)
            if journal_state == "landed":
                journal.record_hash(row["operation_id"], "0x" + f"{index + 1:064x}", consumed=True)
            plan["calls"][key] = {
                "intent": {"args": [], "kwargs": {}},
                "state": call_state,
                "money": True,
                "composite": False,
                **({"result": [True, "0x" + f"{index + 1:064x}"]} if call_state == "done" else {}),
            }
        # A checkpointed call with no journal operation bound to it. Chain state
        # in the manifest looks consistent with the effect; that is corroboration
        # and the screen must still refuse to call the step done.
        plan["calls"][f"{root}/lend/0"] = {
            "intent": {"args": [], "kwargs": {}}, "state": "started",
            "money": True, "composite": False,
        }
        conn = journal._conn  # noqa: SLF001 - the fixture writer, not the read path
        conn.execute(
            "CREATE TABLE IF NOT EXISTS moonwell_run"
            " (singleton INTEGER PRIMARY KEY CHECK(singleton=1), run_id TEXT NOT NULL)"
        )
        conn.execute("INSERT OR REPLACE INTO moonwell_run VALUES (1,?)", (run_id,))
    finally:
        journal.close()

    _write_checkpoint(directory / "run-plan.json", plan)
    _write_manifest(directory, root)


def _write_checkpoint(path: Path, data: dict) -> None:
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path.write_text(json.dumps({"data": data, "sha256": digest}, sort_keys=True, indent=2))


def _write_manifest(directory: Path, root: str) -> None:
    manifest = {
        "stateDir": ".",
        "run_id": root.split("/", 1)[0],
        "scenario": "fixture",
        "mode_labels": FIXTURE_MODE_LABELS,
        "not_proven": [
            "everything: this is a fixture and proves nothing about any system",
        ],
        "wallet": WALLET,
        "chainId": CHAIN_ID,
        "corroboration": {
            f"{root}/lend/0": [
                {
                    "label": "mwstETH balance rose by the expected amount",
                    "value": "50000000000000000",
                    "source": "fixture chain reading (corroboration only)",
                }
            ]
        },
        "position": {"block": "0x0", "nonce": 4, "wallet": {"eth_wei": 0}},
    }
    (directory / "run-manifest.json").write_text(json.dumps(manifest, indent=1))
