# Command map

This snapshot has a distinct runner: `integration/scripts/judge_checks.py`.
It retains the first eight legacy commands and replaces the former combined
upstream step with explicit SDK-only reconstruction and tests. A passing snapshot
suite must not be described as the former runner passing 9/9.

| Former name | Snapshot name / coverage |
|---|---|
| reconcile-help | Same script and arguments |
| integration | All original integration test bodies, including six legacy scheduler controls |
| hosted | All original hosted tests |
| moonwell | All original Moonwell tests |
| console | All original console tests; HTTP handler tests use AF_UNIX |
| moonwell-gate | Same gate script |
| calldata | Same calldata script |
| mutations | Same 26 named pairs and unchanged mutation/test bodies |
| patches | `sdk-snapshot`: same SDK base/three patches/eight hashes, executor + Aave tests, SDK contract + REC-1 + rejected-observation tests |

The former KeeperHub reconstruction, baseline/patched server checks and three
server mutation controls are unavailable here. They are not skipped successes.
`check_upstream_patches.py` now emits UNAVAILABLE and exits 3. The legacy scheduler
remains for its unchanged tests and imports used by the mutation runner; its
last step therefore fails explicitly if run. Its SDK default resolves the
installed editable SDK, and its unused KeeperHub tree precondition is removed.
No stub pretends to implement server behavior. Existing assertions are unchanged.
