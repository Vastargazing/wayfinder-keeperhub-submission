# Third-party notices

The SDK patches contain context and modifications to Wayfinder Paths SDK source
at `46dbf7c05e7f17e6c10a136da0dae6e13e590e95`. Its license is MIT,
Copyright (c) 2024 Wayfinder. The complete upstream notice is preserved verbatim
in [Wayfinder-LICENSE.txt](third-party/Wayfinder-LICENSE.txt).

The SDK and installed dependency distributions are not included in this archive.
Their licenses remain applicable when acquired separately. `requirements.lock`
records versions, not a grant of rights. Original project code, tests,
documentation and new release scripts are licensed under the Apache License,
Version 2.0 (see LICENSE and NOTICE); that grant does not extend to the
third-party material described in this file or to separately acquired
dependencies.

`integration/keeperhub_executor/provenance.py` contains a JSON-domain Python port
of KeeperHub `lib/utils/redact.ts` at
`57b7be4d0b4149b47600f5cd2c55299f4f120e83` (pinned source SHA256
`721d17bb32d959c140e15a68c0b1e4a3e6f652e6c55b9e55b69c14f90682a61d`).
The port changes language/implementation while retaining the source policy.
KeeperHub's upstream [copyright/license notice](third-party/KeeperHub-NOTICE.txt)
identifies Copyright 2025 Vercel, Inc. and Apache License 2.0. The complete
[Apache 2.0 text](third-party/Apache-2.0.txt) is included. This notice does not
license all original project work or imply a server patch is distributed.

`integration/scripts/check_redaction_policy.py` is an optional source-equivalence
helper requiring separately acquired exact KeeperHub source and Node with
TypeScript stripping/VM modules. It is outside the snapshot runner, and its
upstream equivalence check has not been rerun for this artifact. The local
provenance tests and mutations remain included. The main snapshot suite has no
Node or KeeperHub-source prerequisite.
