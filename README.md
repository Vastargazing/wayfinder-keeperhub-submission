<img src="assets/keeperhub.svg" alt="Wayfinder Paths — KeeperHub" width="640">

# Wayfinder Paths / KeeperHub source snapshot

This repository contains a strategy built on the real Wayfinder Paths SDK, with KeeperHub used as the external transaction executor.

The main goal is safe recovery around uncertain transaction outcomes. Before sending a transaction, the client persists the operation identity. If it can no longer determine what happened, it stops instead of guessing or submitting again. The operator console then shows what is known, where execution stopped, and what evidence is needed before it can continue.

If you're reviewing the project, start with [reproduction](docs/reproduction.md), [architecture](docs/architecture.md), and the [evidence index](evidence/INDEX.md).

This snapshot is based on commit `5d41778ca3d84839efe0c5df810553c251c0c744`. Release-specific documentation and verification changes are recorded in [provenance](SOURCE-PROVENANCE.json).

The four product packages and their existing tests are preserved byte-for-byte from the baseline. Three SDK patches reconstruct the accepted journal and external-executor contract. Dependencies and upstream repositories are not bundled and must be obtained separately.

The supported runtime for this snapshot is offline/model.

There are a few important limits to what the included evidence proves:

* Historical Base Sepolia mint usage is included as a redacted summary, but it is not evidence of a new Aave approve → supply → withdraw cycle.
* Live crash recovery and multiwriter fencing have not been verified against the current code.
* Refusal of direct fallback by the deployed server has not been verified.
* The offline models do not execute EVM transactions or Turnkey operations.

This snapshot also has its own `judge_checks.py` suite. The SDK-only ninth step is not the old combined upstream validation. See the [command map](docs/command-map.md) for the exact mapping.

The local operational launcher and historical stand are not part of this delivery.

The [public tuple/overload contribution](docs/submission-evidence.md) is a separate bounty contribution. The PR was merged upstream, but that alone does not show that the change is running in the hosted deployment.

## License

Original material in this snapshot — the four product packages, their tests, documentation, and release scripts — is licensed under the Apache License, Version 2.0.

Copyright 2026 Valery Borovsky.

See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Third-party material remains under its original terms. The SDK patches include Wayfinder Paths context licensed upstream under MIT, Copyright (c) 2024 Wayfinder. The redaction port in `integration/keeperhub_executor/provenance.py` is Apache-2.0, Copyright 2025 Vercel, Inc.

Nothing in this repository changes or grants additional rights to that third-party material, the SDK itself, or separately acquired dependencies. See [third-party notices](THIRD-PARTY-NOTICES.md) for details.

## Verifying the file inventory

`python3 integration/scripts/verify_snapshot.py` checks every distributed file
against [SNAPSHOT-MANIFEST.json](SNAPSHOT-MANIFEST.json) and reports anything
else in the tree as an unlisted file. Run it directly in a Git clone:

```
python3 integration/scripts/verify_snapshot.py
```

Exit 0 means every listed file is present with its recorded hash and nothing
else is there. The only path it skips is the clone's own root `.git` directory,
recognised by name and type without reading it: checkout metadata, not
distributed content. Hidden files, symlinks, `.git` directories anywhere else,
a root `.git` symlink and a root `.git` file (worktree or submodule layout, not
supported) are all refused. After installation the tree also holds generated
files; see [inventory checks](docs/reproduction.md#snapshot-inventory-checks)
for `--prepared-sdk`.

Checking an export instead of the clone is optional. Extract into a new, empty
directory each time rather than into an existing one:

```
SNAPSHOT_EXPORT="$(mktemp -d ../snapshot.XXXXXX)"
git archive --format=tar HEAD | tar -x -C "$SNAPSHOT_EXPORT"
python3 "$SNAPSHOT_EXPORT/integration/scripts/verify_snapshot.py"
```
