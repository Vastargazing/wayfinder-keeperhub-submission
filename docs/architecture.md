# Architecture and recovery boundaries

## Source

The patched SDK persists an operation identifier and unsigned envelope before
calling an external executor. The executor owns nonce selection and submission.
`integration/keeperhub_executor/executor.py` implements transport, ownership
checks and lookup; `authorization.py` binds results to the original SDK journal.
The SDK patch and `integration/tests/test_sdk_contract.py` specify their contract.

`landed`, `never_seen`, and `indeterminate` are different answers. The current
KeeperHub `_no_execution` path returns indeterminate: an absent record or HTTP
404 cannot establish request quiescence. A hash alone is neither a successful
receipt nor a completed user goal. The client preserves an unresolved operation
and refuses a new send; it does not switch executors after an unknown outcome.

## Original operation and fresh permission

The SDK journal stores operation identity, envelope and state before transport.
A consumed result returns through its bound durable step. Balance or allowance
cannot substitute for ownership. Hosted recovery separately verifies the
transaction envelope, effective sender, receipt events and execution provenance.
Accepted historical proof survives a failed fresh check, while permission to
change the journal is invalidated. See `hosted-sepolia/tests/test_rec1.py`,
`test_rejected_observations.py` and `integration/tests/test_authorization.py`.

Moonwell `RunPlan` and the durable driver retain call identities, quotes,
allowance decisions and child sends. Unknown results, unbound expired quotes,
incomplete seed and inconsistent checkpoints stop. Product lifecycle tests and
26 mutation pairs exercise these refusals; they use synthetic HTTP/RPC.

## Operator view

`console/operator_console` reads the run plan and journal, binds the manifest to
that run and displays evidence provenance. `actions.py`, `audit.py` and tests
constrain the page to read actions. Read-only SQLite can create or update WAL/SHM;
it is not an unchanged-directory promise. Fixture creation refuses occupied
paths, and status refuses missing state instead of creating it.

## Limits

This is single-writer local/model validation. It does not establish multiwriter
fencing, universal exactly-once execution, hostile in-process isolation or a
server deployment guarantee. AF_UNIX tests exercise HTTP parser/handler behavior,
not creation and binding of the application's TCP listener. Successful ordinary
`run_loop.py status` reads RPC; the shipped offline demonstration substitutes an
explicit synthetic Chain. The current-code live Aave cycle remains UNVERIFIED.
