# Wayfinder Paths / KeeperHub source snapshot

A strategy uses the real Wayfinder Paths SDK and KeeperHub as its external
transaction executor. The client saves operation identity before sending and
stops when the outcome is uncertain. The operator console shows what is known,
where the strategy stopped and what evidence is needed to continue.

Start with [reproduction](docs/reproduction.md), [architecture](docs/architecture.md)
and the [evidence index](evidence/INDEX.md). This is a source snapshot based on
`5d41778ca3d84839efe0c5df810553c251c0c744`, with release-only documentation and
verification adaptations recorded in [provenance](SOURCE-PROVENANCE.json).

The four product packages and all their included existing tests retain their
baseline bytes. The three SDK patches reconstruct the accepted journal and
external-executor contract. Dependencies and upstream repositories are acquired
separately; they are not bundled. The supported runtime here is offline/model.

Historical Base Sepolia mint consumption is documented by a redacted summary.
It does not demonstrate a new Aave approve/supply/withdraw cycle. Current-code
live crash recovery, multiwriter fencing, and deployed server refusal of direct
fallback remain UNVERIFIED. Offline models do not execute EVM or Turnkey.

This snapshot uses a separate `judge_checks.py` suite. Its SDK-only ninth step
is not the former combined upstream validation. See [command map](docs/command-map.md).
The local operational launcher and historical stand are outside this delivery.
The [public tuple/overload contribution](docs/submission-evidence.md) is a separate
bounty contribution; a merged PR does not establish hosted deployment.

## License

The original material in this snapshot — the four product packages, their tests,
the documentation and the release scripts — is licensed under the Apache License,
Version 2.0. Copyright 2026 Valery Borovsky. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Third-party material keeps its own terms. The SDK patches carry Wayfinder Paths
context under upstream MIT, Copyright (c) 2024 Wayfinder; the redaction port in
`integration/keeperhub_executor/provenance.py` carries Apache-2.0, Copyright 2025
Vercel, Inc. This license does not grant or change rights in that material, in the
SDK itself or in dependencies, which are acquired separately under their own terms.
See [third-party notices](THIRD-PARTY-NOTICES.md).

## Verifying the file inventory

`python3 integration/scripts/verify_snapshot.py` checks every distributed file
against [SNAPSHOT-MANIFEST.json](SNAPSHOT-MANIFEST.json). It compares a directory
against that inventory, so in a Git clone it also reports the clone's own `.git`
files as unlisted extras and exits non-zero. To check the published files only,
run it on an export of the tree:

```
git archive --format=tar HEAD | (mkdir -p ../snapshot && tar -x -C ../snapshot)
python3 ../snapshot/integration/scripts/verify_snapshot.py
```
