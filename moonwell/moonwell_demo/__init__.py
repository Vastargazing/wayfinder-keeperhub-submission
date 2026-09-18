"""Moonwell wstETH loop demo: real strategy -> seam -> KeeperHub -> Base fork.

Nothing in this package is on the send path. Every fund-moving transaction is
built by the SDK's own adapter code and submitted by the KeeperHubExecutor from
``research/integration/keeperhub_executor`` (owned elsewhere, imported unchanged).
What lives here is:

* ``lifi``   — a keyless quote source, a labelled STAND-IN for BRAP.
* ``stubs``  — read-only stubs for TOKEN_CLIENT and the APR lookup.
* ``plan``   — the driver's own step checkpoint (why: see README §"resume").
* ``gate``   — the operator reconciliation gate.
* ``wiring`` — assembly of strategy + seam + executor.
"""
