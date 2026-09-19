# Reproduce this snapshot

Python 3.12, Git and uv are prerequisites. Work from the root of a Git clone of
this repository or of an extracted archive; either one is `SNAPSHOT` below.
Use a separate writable directory (`WORK`) outside it for venv, cache, temporary
state and results. Reconstruct the SDK physically inside this tree at
`wayfinder/upstream`: existing test origin assertions require this layout.
The generated SDK is not part of the source archive; keep the sealed export and
archive untouched. This task used a new
offline venv and a private copy of an available dependency cache, with no downloads.
That does not prove a new network installation or installation of Python/uv.

## Prepare and install

Obtain the SDK Git object at `46dbf7c05e7f17e6c10a136da0dae6e13e590e95` separately
from https://github.com/WayfinderFoundation/wayfinder-paths-sdk . `SDK_OBJECTS`
is a local repository containing that exact object. Preparation does not fetch.
`WORK` must be outside this source tree. `SNAPSHOT` is the absolute root of the
clone or extraction. Set `SDK="$SNAPSHOT/wayfinder/upstream"`; this must be a
physical directory, not a symlink to an external SDK. Paths below are
user-selected absolute paths. Before installing anything, run the
[inventory check](#snapshot-inventory-checks) once from `SNAPSHOT`.

What must exist before the commands, and what they create:

- `SDK_OBJECTS`: an existing local Git repository that already contains commit
  `46dbf7c05e7f17e6c10a136da0dae6e13e590e95`. `prepare_sdk_snapshot.py` reads
  it with `git archive` and never fetches.
- `$WORK/cache`: a uv cache that already holds every wheel named in
  `requirements.lock`. `--offline` installs from this cache only; a missing
  entry is a failure, not a download. Using a locally available cache is not
  a network installation and does not demonstrate one.
- `$SDK`, `$WORK/recipe` and `$WORK/user-path` must not exist yet; the scripts
  create them and refuse an existing directory. `uv venv` creates `$WORK/venv`;
  `judge_checks.py` creates `$WORK/results`. Create `$WORK/tmp` yourself before
  the check commands.
- Inside `SNAPSHOT` the commands create only the generated `wayfinder/upstream`
  and the `*.egg-info` metadata of the editable installs; both are accounted for
  by `verify_snapshot.py --prepared-sdk`. In a Git clone they appear as untracked
  or ignored paths; the tracked files stay unchanged.

```sh
python3 integration/scripts/prepare_sdk_snapshot.py --objects "$SDK_OBJECTS" --dest "$SDK" --out "$WORK/recipe"
uv venv --offline --python python3.12 "$WORK/venv"
UV_CACHE_DIR="$WORK/cache" uv pip install --offline --python "$WORK/venv/bin/python" -r requirements.lock
UV_CACHE_DIR="$WORK/cache" uv pip install --offline --no-deps --python "$WORK/venv/bin/python" -e "$SDK" -e integration -e hosted-sepolia -e moonwell -e console
uv pip check --python "$WORK/venv/bin/python"
```

The preparation applies the three pinned patches to `$SDK` itself, in a Git clone
exactly as in an extraction. The patch commands are confined to that directory:
a repository around the snapshot is neither consulted nor changed, no repository
is created inside `$SDK`, and no `GIT_CEILING_DIRECTORIES`, `GIT_DIR` or other
Git variable has to be set — one already in the environment is ignored for those
commands only. If the destination still resolves into a repository, the script
stops with a nonzero exit naming that directory instead of writing a partly
patched SDK. `check_sdk_snapshot.py` reconstructs its own copy the same way,
including when `TMPDIR` is inside a clone. The recipe output records the
discovery check next to each patch log.

For a 1 GiB memory limit, install each noncomment line of `requirements.lock`
sequentially with `--offline --no-deps`, then install the five editable packages
separately and run `uv pip check`. Missing cache entries are blockers. Check the
editable maps and actual imports: the four project modules must resolve into
this extracted tree and `wayfinder_paths` into the reconstructed SDK.

## Snapshot inventory checks

Before installation, from `SNAPSHOT`, the tree must contain only the listed files:

```sh
python3 integration/scripts/verify_snapshot.py
```

This runs in a Git clone as well as in an extraction. The clone's own root
`.git` directory is the only path skipped, by name and type and without reading
it; any other unlisted path, hidden file, symlink or nested `.git` is refused
with exit 1 and named in the JSON `errors`. A root `.git` that is a symlink or a
file (worktree layout) is refused as unsupported. After
[Prepare and install](#prepare-and-install) the tree additionally holds
`wayfinder/upstream` and `*.egg-info` directories; from then on run
`verify_snapshot.py --prepared-sdk`, which accounts for exactly those two kinds
of generated paths and also checks the eight recipe targets of the generated
SDK. Keep `PYTHONDONTWRITEBYTECODE=1` set as below, otherwise `__pycache__`
directories appear as unlisted files.

## Snapshot check commands

```sh
export PYTHONDONTWRITEBYTECODE=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export PYTEST_PLUGINS=pytest_asyncio.plugin,pytest_mock
export PYTEST_ADDOPTS="--asyncio-mode=auto -p no:cacheprovider"
export TMPDIR="$WORK/tmp"
"$WORK/venv/bin/python" integration/scripts/judge_checks.py --sdk "$SDK" --sdk-objects "$SDK_OBJECTS" --out "$WORK/results" --timeout moonwell=1200 --timeout mutations=1800 --mutation-timeout 120
"$WORK/venv/bin/python" integration/scripts/snapshot_user_path.py --out "$WORK/user-path"
"$WORK/venv/bin/python" integration/scripts/verify_snapshot.py --prepared-sdk
```

Create the external tmp directory first. The runner executes all named commands
in [the command map](command-map.md), preserves the first failure and records
raw commands, exits, process cleanup, JUnit and origins. SDK checks reconstruct
another copy from the exact object and compare eight target hashes without
regenerating expected data. The 26 mutation pairs are unchanged. The finite
budgets are 240 seconds normally, 1200 for Moonwell, 1800 for the mutation matrix
and 120 per control/mutant. A timeout is failure, not mutation detection.
This is a distinct suite, not the former nine-command combined upstream suite.
The unavailable former checker reports exit 3 explicitly.

For local reproduction, all product imports and children were enclosed by a
Linux Landlock read allowlist for this package and interpreter/system libraries;
a deny-probe of the original workspace README succeeded before product imports.
An external supervisor imposed 1 GiB RAM, swap 0, CPU100%, Tasks64 and AF_UNIX;
start gate 2 GiB available, cutoff 1.5 GiB. These are execution controls, not
properties automatically set by the command above. No network access is needed.
Source installation may create packaging metadata in the extracted tree; these
files and the explicitly prepared `wayfinder/upstream` source are not part of
the archive inventory. `verify_snapshot.py --prepared-sdk` reports those
separately and checks the eight recipe targets; the dedicated reconstruction
check remains the SDK behavior witness. Product tests write only disposable
state/results; the export and archive remain sealed.

## Synthetic CLI demonstration

`snapshot_user_path.py` runs real fixture creation, occupied-path refusal, HTML
rendering, missing-state status refusal and successful status/manifest rendering.
Successful status substitutes an explicitly synthetic Chain; it does not call
RPC. Primary fixture bytes are checked independently from WAL/SHM sidecars.
It saves output files and origins and binds the actual producer's run_id/stateDir
through the console consumer. The generated hashes are model evidence and must
not be turned into explorer links.

## Recovery scope

`RunPlan` v2 keeps a run ID, iteration intent, calls and results. The SDK journal
binds each run/iteration/call/send index to one operation and original envelope.
The plan uses atomic replacement, fsync and an exclusive process lock. A second
writer fails before execution. Missing, malformed, legacy or inconsistent plans
are refused; there is no automatic legacy migration.

The real Moonwell strategy continues after an interrupted borrow or wrap,
including a consumed SDK hash whose driver result was not saved. Completed
monetary calls and prior balance reads are replayed from the checkpoint. Borrow
balance reads use the receipt block, and the gas reserve is saved. The adapter
still checks receipts; a known hash is not a successful transaction or a goal.

Interrupted approve, swap, lend and collateral calls continue through named
child checkpoints. The whole upstream `_swap_with_retries` loop is not replayed:
resume enters its saved BRAP adapter call. The complete quote and explicit expiry
are retained; allowance observations and the approve/reset branch are saved before
sending. Each monetary child requires the journal operation bound to its exact
step ID, including when returning a completed result. Chain state verifies effects
but cannot establish operation ownership.

An unresolved operation still stops with `CheckpointError`, without a new send or
quote. An expired quote may recover its original validated landed operation. If
no swap operation is bound, an expired saved quote stops before approval or swap.
The pinned BRAP schema has `createdAt` but no required expiry; the driver records
explicit `expires_at`, `expiresAt` or nested `quote.deadline` as Unix seconds.
Missing expiry stays unknown and blocks a resumed, unbound swap. It does not invent
a TTL from `createdAt`. A quote request interrupted before its result checkpoint
also stops, since requesting again would violate the one-request boundary.
New quotes require a separate operator decision; there is no automatic requote UI.
Legacy incomplete aggregate calls without named children remain refused.

An incomplete seed intent also
cannot be treated as completed or replayed automatically. `--skip-seed` records
an explicit external-collateral assertion, separately from a confirmed seed.

## Run status

Read-only describes the local run-state access, not an offline command. After
validating an existing checkpoint and journal, `status` reads the configured RPC
to check the Base Anvil fork, position and recorded calldata. It therefore needs
a reachable authorized fork; do not point it at a synthetic fixture and expect
a network-free inspection. Missing or invalid local state is refused before
these chain reads. For a local screen with no network calls, first complete
[Prepare and install](#prepare-and-install), keeping the same user-selected
absolute `WORK` and `SNAPSHOT`. Run from the extracted `SNAPSHOT` root.
Set `DIR` to the absolute path of your synthetic fixture or previously prepared
offline demonstration state; this example does not create or require live state.
The output below is outside both the source tree and the state directory.

```sh
cd "$SNAPSHOT"
"$WORK/venv/bin/python" -m operator_console --state-dir "$DIR" --render "$WORK/screen.html"
```

Offline status tests model `Chain`
while exercising the actual parser, report producer and console consumer; their
chain values are synthetic.

`moonwell/scripts/run_loop.py status --state-dir DIR [--out FILE]` describes a run
that already exists. It reads `DIR/run-plan.json` and `DIR/sdk-journal.sqlite` and
creates neither: no state directory, no checkpoint, no `run-plan.lock`, no SDK
journal, and no legacy checkpoint is migrated. A missing, damaged, unreadable or
foreign run is reported as unavailable with exit 2 instead of being created. For
the same reason `status` also answers while the run's own writer holds the
checkpoint lock.

The checkpoint and the journal are two reads, not one transaction. The
checkpoint's data and the digest that identifies them come from a single read;
the file is read again after the journal, and a change between the two readings
is refused rather than published, so a report never mixes two versions of a run.
SQLite is opened `mode=ro`, which makes SQLite itself refuse a write to the
database and refuse to create it; that is not a promise of an unchanged
directory, because a read-only read may still create or update the
`-wal`/`-shm` sidecars.

A shared run id is not enough to call the two files one run. Before reporting
success, `status` also requires that every journal operation has its driver step
binding, that every binding names an operation the journal still holds and a call
the checkpoint still has, and that a call recorded as a finished monetary one has
the submitted operation and matching transaction hash behind it. Where they
disagree the run is reported as unavailable; nothing is reconstructed or
repaired.

A described run saves its own `run_id` — the checkpoint's, once the journal's
`moonwell_run` identity is confirmed to agree — and a `stateDir` relative to the
report's own directory, which is how `console/operator_console/sources.py`
resolves it, so a state directory and its report can be moved together. The
report never lands inside the state directory it describes, and never on a file
of it reached through an existing hardlink: such an `--out` is refused before
anything is written. An `--out` whose final component is a symlink is refused
outright rather than followed, because the report would then live somewhere other
than where it was named while the reader resolves the saved `stateDir` from the
directory of the file it is handed; a symlinked parent directory is fine, since
producer and reader resolve it the same way. What is checked and what is written
are the same resolved path, so an `--out` containing `..` cannot create a
directory the check already normalised away. That is a rule about the path given,
checked once; it is not a sandbox against the filesystem being rearranged
underneath an open file. A refusal, when it writes a file at all, saves no
`run_id`, so the console reports it as unbound instead of mistaking it for a run
manifest.


### Resume process status

`run_loop.py resume` exits nonzero when the saved result is unsuccessful,
including no in-flight iteration or an unsuccessful requested `--continue-loop`.
It saves evidence and closes the writer before returning that status. A successful
resume exits zero. The deliberate `recheck` demonstration keeps its existing
contract: inspect its result and send count to determine whether uncertainty was
refused. An error string alone is not the process-status policy.
