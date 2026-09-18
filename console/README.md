# The operator screen

One page that answers, from a run's own artifacts, the four questions an
operator actually has:

1. **The run** — strategy, run id, which iteration, and the ordered money steps.
2. **Per operation** — the three-valued outcome (`landed` / `never_seen` /
   `indeterminate`), the transaction hash where there is one, and the source of
   every claim.
3. **Where execution stopped, and why**, in words that name what happened.
4. **What is needed to continue**, concretely — "this operation's fate is
   unknown; check its state", not a vague error.

The differentiator this screen exists to show is that the system refuses to
guess and says what it needs. The honest stall is the product, so the screen
renders the stall as a first-class result rather than as a failure.

## The interaction rule

**There is no retry, resend or re-submit action anywhere** — not disabled, not
behind a confirmation, not present. When an outcome is indeterminate the primary
action is **Check state**: re-read the journal, the checkpoint and the manifest
and re-derive every outcome from what they say now.

That rule is enforced in three places rather than asserted in a comment:

- `actions.py` — the registry cannot construct an action that is not a GET read
  to one of two routes.
- `audit.py` — `audit_document` runs over the finished HTML on **every** render
  and raises if the page contains a form, a button, script, an unknown link
  target, or clickable text offering to send, retry, sign, resolve or force
  anything. A page with a send affordance in it is never returned.
- `server.py` — `do_GET` and `do_HEAD` exist; `POST`, `PUT`, `PATCH` and
  `DELETE` are answered `405` with an explanation.

The journal is opened with SQLite's `mode=ro` URI plus `PRAGMA query_only`, so
SQL data writes through that connection are refused by SQLite itself. Reading a
WAL-mode database can still create or update SQLite WAL/SHM auxiliary files;
read-only does not promise an unchanged directory. It also does not imply that
every read changes an existing SHM. An auxiliary file's mtime establishes neither
a monetary write nor who accessed it. The checkpoint is read once as bytes, parsed and hashed from that same buffer;
its stored checksum and v2 structure are checked separately. `RunPlan` is deliberately not imported, because opening one
takes an exclusive lock and writes a lock file.

## Labelling

Every displayed value carries a provenance chip — `SOURCE`, `OBSERVED`,
`INFERRED` or `UNVERIFIED` — and names the artifact and column it came from. A
`Fact` cannot be constructed without an origin.

**Mode labels are never carried between runs.** They come from a run manifest
and only when its saved path and run identity bind to the state being read; otherwise every
mode reads `UNDECLARED` and is tagged `UNVERIFIED`. The screen has no default to
fall back on, so a fork run cannot come to look like a mainnet run and a
dev-signer run cannot come to look like a real-signer run.

**Chain state is corroboration, never proof.** Readings a run attached to a step
are displayed under an explicit heading, and `step_executed` refuses to read
them: a step is executed only when the journal operation bound to its exact step
id landed and the driver recorded the result. An allowance that looks right does
not prove our approve landed, and an unchanged nonce does not prove nothing was
sent.

## Running the offline example

Install the snapshot as described in [reproduction](../docs/reproduction.md).
Then run `integration/scripts/snapshot_user_path.py --out /absolute/new/output`
with that environment's Python. The resulting HTML can be opened as a local
file. The output is labelled synthetic; it is not a chain execution witness.


## Tests

Complete [Prepare and install](../docs/reproduction.md#prepare-and-install)
first, and keep the same user-selected absolute `WORK` and `SNAPSHOT` paths.
Run from the extracted `SNAPSHOT` root with the installed pytest plugins and
asyncio settings from [Snapshot check commands](../docs/reproduction.md#snapshot-check-commands).
Set these explicitly; an activated environment is not required.

```sh
cd "$SNAPSHOT"
export PYTHONDONTWRITEBYTECODE=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export PYTEST_PLUGINS=pytest_asyncio.plugin,pytest_mock
export PYTEST_ADDOPTS="--asyncio-mode=auto -p no:cacheprovider"
env -u PYTHONPATH "$WORK/venv/bin/python" -m pytest console/tests -o addopts='' -q
```

`test_console_actions.py` proves the interface exposes no send-capable action and
no retry affordance, and that an indeterminate operation renders "Check state" as
the primary action. `test_console_readonly.py` checks that the fixture's main journal file and
checkpoint remain byte- and mtime-identical after rendering, that SQLite refuses
a write on the connection string used, and that the tested write methods are
refused. This does not check unchanged WAL/SHM files or an unchanged directory. The HTTP cases
use real `http.client` and `ConsoleHandler` over `socketpair(AF_UNIX, SOCK_STREAM)`,
with bounded timeouts and handler-thread errors propagated to the test. They
check HTTP parsing and responses without an IP socket. Creating, binding and
running the TCP listener through `make_server` are a separate boundary, not
covered by this suite. Constructor-boundary tests check host validation before
server creation; they do not exercise TCP bind or accept.
`test_console_labels.py` proves a fork/dev-signer run is never labelled mainnet or
real signer and that another run's manifest cannot label this one.
`test_console_model.py` proves the three-valued mapping and that chain state
cannot mark a step done.
`test_fixture_creation.py` proves `--make-fixture` refuses every path that is
already taken — a run's state directory, an empty directory, a file, a finished
or half-written fixture, a symlink, a symlinked parent — leaving the files that
were there unchanged and creating no sidecar of its own, that two builders
racing for one free path yield one fixture, and that a failure after the
directory is claimed is reported rather than presented as a build.

Two mutations with positive controls live in
`integration/scripts/offline_mutations.py`: `operator-retry-action` adds a retry
link to the indeterminate card, and `chain-state-marks-step-done` lets
corroboration decide a step's state. Each makes its named test fail.

## Status and source contract

The headline has three model states (`StopReport.status`). `stopped` identifies
an unresolved journal operation or a recorded driver refusal/stop boundary;
`uncertain` identifies unusable or contradictory sources, unknown quote validity,
or unfinished recorded work whose current activity cannot be established;
`clear` means no stop is recorded in the available evidence. The compatibility
flag `blocked` is true for both `stopped` and `uncertain`: it is a constraint on
what the console may conclude, not a liveness probe of the driver process.
Only **Check state**, a GET read, is offered in either case.

Checkpoint checksum, v2 shape, run ownership, monetary result/hash bindings and
completed composite children are checked before using it for driver progress.
An unusable checkpoint supplies no executed-step or completed-iteration claims.
Journal operation observations remain visible with their own evidence boundary.
Seed intent, unfinished iterations and composite calls remain visible even before
a monetary child exists. An interrupted quote request never offers a second
request. Unknown expiry cannot establish safe continuation on reopening. A swap
already bound to its saved operation is not replayed because its quote later
expired. Manifest limitations are informational; they do not by themselves mean
the driver stopped. Neither an empty pending set nor `clear` means the strategy
achieved its goal, and a journal hash is not fresh receipt/position validation.

## Manifest ownership

An absolute `stateDir` must resolve exactly to the directory being read. A relative
`stateDir` is resolved **relative to the manifest file's parent directory**, never
the working directory or an inferred suffix. The saved top-level `run_id` must
match the journal's `moonwell_run.run_id` and any available valid checkpoint.
A malformed checkpoint or conflicting checkpoint/journal bindings also refuses
binding. With no journal identity, binding is unverified. A missing checkpoint
allows a journal-identified manifest declaration, but cannot establish driver
progress. No identity is supplied to the manifest by the reader.

For a portable bundle, its producer explicitly saves, for example,
`{"stateDir":"../state/run","run_id":"<saved run identity>"}` in
`metadata/manifest.json`. Moving the entire bundle preserves that relationship;
old absolute paths do not acquire trust after relocation. Newly generated console
fixtures save this identity. Existing user artifacts are never rewritten.
Unbound manifests supply no mode, position, corroboration, limitations, workflow,
wallet, RPC or other run facts. Bound contents remain **declarations**, including
any Turnkey/testnet label; they are not proof of signer custody or network execution.

## Snapshot identity and consistency

All journal SELECTs use one connection and one explicit read transaction, with
`mode=ro` and `query_only=ON`. The transaction ends immediately after reading and
hashing, and the connection closes on success or error. The console does not
checkpoint/truncate WAL or enable writes.

`JournalSnapshot.sha256` is the SHA256 of the UTF-8 canonical JSON projection
`operator-console-journal-v1`: format tag, sorted table names, operations ordered
by `(created_at, rowid)` with their displayed `seq`, all `OPERATION_COLUMNS`
(including parsed envelope and boolean consumed), step-id/operation-id bindings,
and the singleton driver run identity. JSON uses sorted object keys, compact
separators, ASCII escapes and finite numbers. It is **not** a hash of the main
SQLite file or every table's contents. A changed WAL-backed projection changes
the fingerprint; reading the same logical projection at another path/time does
not. Checkpoint and manifest file hashes each identify exactly the single byte
buffer parsed by their reader; the checkpoint's internal checksum is distinct.

JSON and SQLite are **not an atomic pair**. Readers do not take the driver's lock
or add a new version protocol. Detected identity, binding, completion or result
contradictions yield `uncertain`, suppress driver progress, and refuse manifest
binding. Compatible versions can still have been read at different times; agreement
is not proof of simultaneous capture and never authorizes continuation.

The additional tests in `test_console_lifecycle.py`, `test_console_manifest_binding.py`
and `test_console_snapshot.py` use temporary artifacts and model external calls.
They cover actual lifecycle-writer files, deterministic WAL interleaving, JSON byte
replacement, error cleanup and the rendered read-only action contract. Offline
model checks do not establish fork, hosted, real Turnkey, live mint recovery or a
complete public cycle. Run local tests under the resource limits and offline guard
recorded in the console rework handoff; do not point them at runtime state.

### Local host and fixture metadata

`--host` accepts canonical dotted-decimal IPv4 loopback literals in
`127.0.0.0/8`. The exact name `localhost` maps directly to `127.0.0.1` without
DNS; the default remains `127.0.0.1`. Empty/wildcard addresses, other hostnames,
LAN/public addresses, alternative numeric spellings and IPv6 are refused before
server construction. This server uses IPv4; IPv6 support is not provided.

An existing empty, unreadable or non-UTF8 `FIXTURE.txt` produces a visible read
problem and a warning that the directory may contain synthetic fixture data.
A directory or broken symlink at that marker path is also reported this way.
The reader does not repair or remove the marker. An absent marker does not prove
a run authentic: journal/checkpoint identity and manifest binding still apply.
