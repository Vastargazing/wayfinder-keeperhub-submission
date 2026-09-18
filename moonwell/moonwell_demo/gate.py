"""The operator reconciliation gate (revision 2: ownership **and** content).

Why this was rewritten
----------------------
Revision 1 compared an operator-supplied transaction against the envelope the SDK
journalled before the send — chain id, sender, recipient, calldata, value — and
claimed "there is no bypass". A review defeated it three ways, and the claim has
been retracted:

1. **Content is not ownership.** Operation A ran and is recorded; operation B has
   an identical envelope and never broadcast. Identical operations produce
   *identical envelopes by construction* (SOURCE: two `enterMarkets` rows in
   `state/happy/sdk-journal.sqlite` share digest `614fec9241…`), so A's hash
   matches B's five fields perfectly and revision 1 closed B with it. An already
   consumed hash was not rejected either.
2. **The verdict was not bound to what was verified.**
   ``dataclasses.replace(verdict, candidate_hash=other)`` kept the module-private
   issuer token, and ``authorized_hash()`` returned the substituted hash.
3. **Applying a verdict re-checked nothing.** ``mark_resolved`` looked only at the
   operation id and the ``pending`` state, so a verdict computed against one
   journal applied cleanly against a different journal in which the same
   operation id denoted a different envelope.

The design now implemented
--------------------------
Two independent checks, **both required**, plus three guards.

**1. Positive ownership binding (new; the decisive one).** The candidate hash must
appear in *KeeperHub's own record for this operation id*. The mapping is the one
the integration package establishes (research/integration/README.md §3): the SDK
sends the operation id as the ``Idempotency-Key`` **and** as ``input.operationId``,
so the execution row whose recorded input carries this operation id is the record
for this operation, and its hashes are:

* ``workflow_executions.transaction_hashes`` (entries for the action node/chain),
* ``workflow_execution_logs.output`` / ``output_raw`` ``transactionHash`` for that
  execution's write-contract node,
* ``pending_transactions.tx_hash`` keyed by that ``execution_id``.

A hash that cannot be traced to KeeperHub's record **for this specific operation**
is never accepted, however well it matches. A calldata scan of the chain can only
ever be a *hint*: identical calldata identifies content, not an operation, so
:class:`OwnershipFinding` carries scan results in ``hints`` and they are never
consulted by :meth:`OwnershipFinding.owns`.

Ownership that cannot be *read* is not ownership that is absent. If KeeperHub's
records are unreachable the finding is ``available=False`` and the gate blocks;
there is no operator path that closes an operation on a guess.

**2. Content verification (unchanged).** The same five comparisons against the
envelope the SDK authorized and journalled before the send. This stays because
KeeperHub's own recorded input has been OBSERVED to diverge from what actually
executed (research/integration/README.md §3, §5.6), so ownership alone would
adopt whatever KeeperHub says it sent. Ownership without content match is
refused; content match without ownership is refused.

**3. Consumed-hash rejection.** A hash already bound to a *different* operation —
in this journal, in ``workflow_executions.transaction_hashes``, or in
``pending_transactions`` — is refused. This is a second net, not the first: it is
necessary but *insufficient*, because an old transaction that appears in neither
record still matches five fields and must still be refused. Only ownership
refuses that one.

**4. Verdict integrity.** A verdict is sealed with an HMAC over the exact facts it
was computed from — operation id, candidate hash, envelope digest, journal
identity, every check, every blocker, and the ownership sources. Mutating any of
them (``dataclasses.replace``, ``object.__setattr__``, editing the ``checks``
list) invalidates the seal. The seal is *not* the guarantee, though; see 5.

**5. Apply-time re-verification.** :func:`mark_resolved` does not trust the verdict
it is handed. It re-reads the journal row, re-derives the envelope digest,
re-queries the ownership oracle, re-reads the chain, and re-runs the whole
comparison; the hash it writes comes from that *fresh* verdict, and the write
happens inside ``BEGIN IMMEDIATE`` with the ``pending`` state and the
no-other-row-holds-this-hash condition re-checked one last time. A verdict
computed earlier, elsewhere, or against another journal cannot be applied, and a
forged verdict is useless because forging it does not make the fresh check pass.

The receipt is read and reported and is **deliberately not a criterion**: a
reverted transaction that is owned by this operation and matches all five fields
*is* the authorized operation — it consumed the nonce — so adopting it is
correct, and the SDK then raises on ``status == 0`` in
``wait_for_transaction_receipt`` exactly as for any other send.

Ambiguity stops
---------------
There is no attestation path, no ``--force``, no ``--yes``. When ownership cannot
be established the operation stays ``pending`` and the seam keeps refusing new
sends. An "operator attests out of band" write was considered and rejected: the
state it would have to write (``submitted``) is the same state a verified
resolution writes, so downstream — ``claim``, the audit, the strategy — could not
tell them apart, and the one property worth having here is that a ``submitted``
row means a hash KeeperHub bound to this operation. What an operator can still do
is record a note next to the journal by hand; nothing in this module will turn
that into a resolution.

Where this belongs
------------------
In the executor package (``research/integration/keeperhub_executor``), next to
``lookup`` — see README §7 "Where this belongs". It is implemented here because
that directory is owned by another worker.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

import httpx

#: Bumped whenever the decisive content of a verdict changes shape. Part of the
#: sealed payload, so a verdict issued by an older gate cannot be applied here.
GATE_VERSION = "2-ownership+content"

#: Mirrors ``wayfinder_paths.core.utils.executor.ENVELOPE_KEYS`` (SOURCE:
#: executor.py:57). Duplicated rather than imported so this module can be loaded
#: and tested without the SDK on the path; :func:`envelope_digest` is checked
#: against the journal's own ``digest`` column on every read, which would catch
#: any drift immediately.
ENVELOPE_KEYS = ("chainId", "from", "to", "data", "value")

_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_:.@+-]{1,128}$")


class ReconciliationRefused(RuntimeError):
    """The candidate transaction is not this operation's. Nothing written."""

    def __init__(self, message: str, verdict: "ReconciliationVerdict | None" = None) -> None:
        self.verdict = verdict
        super().__init__(message)


def envelope_digest(envelope: dict[str, Any]) -> str:
    """Stable content hash of an envelope, byte-identical to the seam's."""
    canonical = json.dumps(
        {k: envelope.get(k) for k in ENVELOPE_KEYS},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def journal_key(journal_path: str | Path) -> str:
    """Identity of one journal file. A verdict is bound to the journal it read."""
    return hashlib.sha256(str(Path(journal_path).resolve()).encode()).hexdigest()


# --------------------------------------------------------------------------
# the authorized side: the SDK's journal, written before the send
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizedOperation:
    operation_id: str
    sender: str
    chain_id: int
    state: str
    consumed: bool
    txn_hash: str | None
    envelope: dict[str, Any]
    stored_digest: str
    created_at: float
    journal_path: str

    @property
    def to(self) -> str | None:
        return self.envelope.get("to")

    @property
    def data(self) -> str:
        return self.envelope.get("data") or "0x"

    @property
    def value(self) -> int:
        return int(self.envelope.get("value") or 0)

    @property
    def envelope_from(self) -> str:
        return self.envelope["from"]

    @property
    def envelope_chain_id(self) -> int:
        return int(self.envelope["chainId"])

    @property
    def computed_digest(self) -> str:
        return envelope_digest(self.envelope)

    @property
    def journal_key(self) -> str:
        return journal_key(self.journal_path)


def read_authorized_operation(journal_path: str | Path, operation_id: str) -> AuthorizedOperation:
    """Read the pre-send authorization row. Read-only; opens the journal ro."""
    path = Path(journal_path).resolve()
    if not path.exists():
        raise ReconciliationRefused(f"no journal at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT operation_id, sender, chain_id, envelope, state, txn_hash, consumed,"
            " digest, created_at FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ReconciliationRefused(
            f"operation {operation_id} is not in the journal {path}. There is no "
            "authorization to compare against, so nothing can be resolved."
        )
    return AuthorizedOperation(
        operation_id=row[0],
        sender=row[1],
        chain_id=int(row[2]),
        envelope=json.loads(row[3]),
        state=row[4],
        txn_hash=row[5],
        consumed=bool(row[6]),
        stored_digest=row[7],
        created_at=float(row[8]),
        journal_path=str(path),
    )


def journal_hash_bindings(
    journal_path: str | Path, txn_hash: str, *, excluding: str
) -> list[dict[str, Any]]:
    """Rows in THIS journal that already carry ``txn_hash`` for another operation."""
    path = Path(journal_path).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT operation_id, state, consumed FROM operations"
            " WHERE lower(txn_hash)=lower(?) AND operation_id<>?",
            (txn_hash, excluding),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"where": "sdk journal", "operation_id": r[0], "state": r[1], "consumed": bool(r[2])}
        for r in rows
    ]


# --------------------------------------------------------------------------
# the observed side: whatever the chain says about the candidate hash
# --------------------------------------------------------------------------


class ChainReader(Protocol):
    """Minimal read surface. A Protocol so the negative tests can substitute one."""

    async def chain_id(self) -> int: ...
    async def transaction(self, txn_hash: str) -> dict[str, Any] | None: ...
    async def receipt(self, txn_hash: str) -> dict[str, Any] | None: ...


class RpcChainReader:
    def __init__(self, rpc_url: str, *, timeout: float = 30.0) -> None:
        self.rpc_url = rpc_url
        self._timeout = timeout

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as c:
            body = (
                await c.post(
                    self.rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
            ).json()
        if "error" in body:
            raise RuntimeError(f"{method}: {body['error']}")
        return body["result"]

    async def chain_id(self) -> int:
        return int(await self._rpc("eth_chainId", []), 16)

    async def transaction(self, txn_hash: str) -> dict[str, Any] | None:
        return await self._rpc("eth_getTransactionByHash", [txn_hash])

    async def receipt(self, txn_hash: str) -> dict[str, Any] | None:
        return await self._rpc("eth_getTransactionReceipt", [txn_hash])


# --------------------------------------------------------------------------
# the ownership side: KeeperHub's own record FOR THIS OPERATION
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnedHash:
    """A hash KeeperHub's records bind to this operation id."""

    hash: str
    source: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"hash": self.hash, "source": self.source, "note": self.note}


@dataclass(frozen=True)
class OwnershipFinding:
    """What KeeperHub's records say about one operation id.

    ``available`` is the three-valued part: ``False`` means *the records could not
    be read*, which is never the same as "there is no record". ``owned`` may be
    empty with ``available=True``, and that is a real observation: KeeperHub has
    no hash bound to this operation id.

    ``hints`` exists so the operator can see calldata-scan candidates without the
    gate ever treating one as ownership. :meth:`owns` does not look at it.
    """

    operation_id: str
    available: bool
    owned: tuple[OwnedHash, ...] = ()
    foreign_bindings: tuple[dict[str, Any], ...] = ()
    hints: tuple[dict[str, Any], ...] = ()
    errors: tuple[str, ...] = ()
    detail: str = ""
    execution_id: str | None = None
    ambiguous: bool = False
    #: False when a query that would have revealed a *foreign* binding of the
    #: candidate hash could not be run. "Not scanned" is not "nothing found", so
    #: the consumed-hash check fails rather than passing on incomplete data.
    foreign_scan_complete: bool = True

    def owns(self, txn_hash: str) -> bool:
        if not self.available or self.ambiguous or not isinstance(txn_hash, str):
            return False
        want = txn_hash.lower()
        return any(o.hash.lower() == want for o in self.owned)

    def sources_for(self, txn_hash: str) -> list[str]:
        want = str(txn_hash).lower()
        return sorted({o.source for o in self.owned if o.hash.lower() == want})

    def as_dict(self) -> dict[str, Any]:
        return {
            "operationId": self.operation_id,
            "available": self.available,
            "ambiguous": self.ambiguous,
            "executionId": self.execution_id,
            "owned": [o.as_dict() for o in self.owned],
            "foreignBindings": list(self.foreign_bindings),
            "foreignScanComplete": self.foreign_scan_complete,
            "hints": list(self.hints),
            "errors": list(self.errors),
            "detail": self.detail,
            "hintsAreNotOwnership": (
                "calldata-scan candidates are diagnostics; identical calldata "
                "identifies content, not an operation"
            ),
        }


class OwnershipOracle(Protocol):
    """Answers 'does KeeperHub's record for THIS operation carry this hash?'."""

    async def ownership(self, operation_id: str, candidate_hash: str) -> OwnershipFinding: ...


class KeeperHubSql:
    """Read-only SQL against the stand's Postgres, over ``docker exec psql``.

    KeeperHub exposes ``pending_transactions`` and ``workflow_execution_logs``
    through no HTTP surface, and its executions listing is capped at 50 rows with
    no pagination (SOURCE: research/integration/keeperhub_executor/client.py:27-30),
    which would silently hide an older execution and turn "KeeperHub has no record"
    into a false negative. SQL is uncapped, so ownership is decided on it.

    Every method returns ``None`` when the probe itself could not run. The caller
    must treat that as *unknown*, never as "no record" — the same three-valued
    discipline the executor package applies to its own probes.
    """

    def __init__(
        self,
        *,
        container: str = "upstream-db-1",
        database: str = "keeperhub",
        user: str = "postgres",
        timeout: float = 20.0,
    ) -> None:
        self.container = container
        self.database = database
        self.user = user
        self.timeout = timeout

    def _rows(self, sql: str) -> list[dict[str, Any]] | None:
        try:
            proc = subprocess.run(  # noqa: S603
                ["docker", "exec", "-i", self.container, "psql", "-U", self.user,
                 "-d", self.database, "-tA", "-c", sql],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except Exception:  # noqa: BLE001
            return None
        if proc.returncode != 0:
            return None
        try:
            return json.loads(proc.stdout.strip())
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _agg(inner: str) -> str:
        return (
            "select coalesce(json_agg(row_to_json(t)), '[]'::json)::text from ("
            + inner
            + ") t"
        )

    def executions_for_operation(
        self, workflow_id: str, operation_id: str
    ) -> list[dict[str, Any]] | None:
        if not _ID_RE.match(workflow_id) or not _ID_RE.match(operation_id):
            return None
        return self._rows(self._agg(
            "select id, workflow_id, status, error_code, deleted_at,"
            " input->>'operationId' as operation_id, input->>'network' as network,"
            " transaction_hashes, started_at, completed_at"
            " from workflow_executions"
            f" where workflow_id = '{workflow_id}'"
            f" and input->>'operationId' = '{operation_id}'"
        ))

    def node_logs(self, execution_id: str) -> list[dict[str, Any]] | None:
        if not _ID_RE.match(execution_id):
            return None
        return self._rows(self._agg(
            "select execution_id, node_id, node_type, status, network, deleted_at,"
            " output->>'transactionHash' as output_hash,"
            " output_raw->>'transactionHash' as output_raw_hash,"
            " output->>'chainId' as output_chain_id"
            " from workflow_execution_logs"
            f" where execution_id = '{execution_id}'"
        ))

    def pending_transactions_for_execution(
        self, execution_id: str
    ) -> list[dict[str, Any]] | None:
        if not _ID_RE.match(execution_id):
            return None
        return self._rows(self._agg(
            "select wallet_address, chain_id, nonce, tx_hash, execution_id, status"
            " from pending_transactions"
            f" where execution_id = '{execution_id}'"
        ))

    def pending_transactions_by_hash(self, txn_hash: str) -> list[dict[str, Any]] | None:
        if not _HASH_RE.match(txn_hash):
            return None
        return self._rows(self._agg(
            "select p.wallet_address, p.chain_id, p.nonce, p.tx_hash, p.execution_id,"
            " p.status, e.input->>'operationId' as operation_id, e.workflow_id"
            " from pending_transactions p"
            " left join workflow_executions e on e.id = p.execution_id"
            f" where lower(p.tx_hash) = lower('{txn_hash}')"
        ))

    def executions_carrying_hash(self, txn_hash: str) -> list[dict[str, Any]] | None:
        if not _HASH_RE.match(txn_hash):
            return None
        return self._rows(self._agg(
            "select id, workflow_id, status, input->>'operationId' as operation_id,"
            " transaction_hashes"
            " from workflow_executions"
            f" where lower(transaction_hashes::text) like lower('%{txn_hash}%')"
        ))


class KeeperHubOwnershipOracle:
    """Ownership from KeeperHub's records, and nothing else.

    Built on :class:`KeeperHubSql`. The optional ``client`` (a
    ``keeperhub_executor.KeeperHubClient``) is used only as a *cross-check* of the
    id → execution mapping over the HTTP surface the integration package uses; a
    disagreement is an ambiguity and blocks, it never adds ownership.
    """

    def __init__(
        self,
        *,
        workflow_id: str,
        chain_id: int,
        wallet_address: str,
        sql: KeeperHubSql | None = None,
        client: Any | None = None,
        action_node_id: str = "action-1",
    ) -> None:
        self.workflow_id = workflow_id
        self.chain_id = int(chain_id)
        self.wallet_address = wallet_address.lower()
        self.sql = sql if sql is not None else KeeperHubSql()
        self.client = client
        self.action_node_id = action_node_id

    async def ownership(self, operation_id: str, candidate_hash: str) -> OwnershipFinding:
        errors: list[str] = []
        owned: list[OwnedHash] = []
        foreign: list[dict[str, Any]] = []

        rows = self.sql.executions_for_operation(self.workflow_id, operation_id)
        if rows is None:
            return OwnershipFinding(
                operation_id,
                available=False,
                errors=("KeeperHub's execution records could not be read (psql probe "
                        "unavailable); absence of a record cannot be inferred from a "
                        "probe that did not run",),
                detail="ownership unknown: KeeperHub is unreadable",
            )
        live = [r for r in rows if not r.get("deleted_at")]
        if len(live) > 1:
            return OwnershipFinding(
                operation_id,
                available=True,
                ambiguous=True,
                errors=tuple(f"execution {r.get('id')} also carries this operation id" for r in live),
                detail=(f"{len(live)} executions carry operationId {operation_id}; the "
                        "idempotency key should have made that impossible, so no hash "
                        "can be attributed to this operation"),
            )

        execution_id: str | None = None
        if live:
            execution = live[0]
            execution_id = execution.get("id")
            if str(execution.get("network")) != str(self.chain_id):
                errors.append(
                    f"recorded execution network {execution.get('network')!r} is not "
                    f"chain {self.chain_id}"
                )
            # source 1 — workflow_executions.transaction_hashes
            hashes = execution.get("transaction_hashes") or []
            if isinstance(hashes, str):
                try:
                    hashes = json.loads(hashes)
                except Exception:  # noqa: BLE001
                    hashes = []
                    errors.append("transaction_hashes is not JSON")
            if isinstance(hashes, list):
                for entry in hashes:
                    if not isinstance(entry, dict):
                        errors.append("malformed transaction_hashes entry")
                        continue
                    if entry.get("nodeId") != self.action_node_id or entry.get("chainId") != self.chain_id:
                        errors.append("wrong node/chain in transaction_hashes entry")
                        continue
                    h = entry.get("hash")
                    if isinstance(h, str) and _HASH_RE.match(h):
                        owned.append(OwnedHash(
                            h.lower(),
                            "workflow_executions.transaction_hashes",
                            f"verified={entry.get('verified')} receiptStatus={entry.get('receiptStatus')}",
                        ))
                    else:
                        errors.append("malformed hash in transaction_hashes")
            else:
                errors.append("transaction_hashes is not a list")

            # source 2 — the write-contract node's own log output
            logs = self.sql.node_logs(execution_id) if execution_id else None
            if logs is None:
                errors.append("workflow_execution_logs could not be read")
            else:
                for lg in logs:
                    if lg.get("deleted_at") or lg.get("node_id") != self.action_node_id:
                        continue
                    if lg.get("node_type") != "web3/write-contract":
                        errors.append("action node has an unexpected node_type")
                        continue
                    for key in ("output_hash", "output_raw_hash"):
                        h = lg.get(key)
                        if not h:
                            continue
                        if not _HASH_RE.match(str(h)):
                            errors.append(f"malformed hash in node log {key}")
                            continue
                        chain_ok = str(lg.get("output_chain_id") or lg.get("network")) == str(self.chain_id)
                        if not chain_ok:
                            errors.append("wrong chain in hash-bearing node log")
                            continue
                        owned.append(OwnedHash(
                            str(h).lower(),
                            f"workflow_execution_logs.{key}",
                            f"nodeStatus={lg.get('status')} RECEIPT NOT VERIFIED",
                        ))

            # source 3 — pending_transactions keyed by execution_id
            pend = self.sql.pending_transactions_for_execution(execution_id) if execution_id else None
            if pend is None:
                errors.append("pending_transactions could not be read")
            else:
                for row in pend:
                    if (row.get("execution_id") != execution_id
                            or row.get("chain_id") != self.chain_id
                            or str(row.get("wallet_address", "")).lower() != self.wallet_address):
                        errors.append("wrong execution/wallet/chain in pending_transactions row")
                        continue
                    h = row.get("tx_hash")
                    if isinstance(h, str) and _HASH_RE.match(h):
                        owned.append(OwnedHash(
                            h.lower(),
                            "pending_transactions.tx_hash",
                            f"nonce={row.get('nonce')} rowStatus={row.get('status')} RECEIPT NOT VERIFIED",
                        ))
                    else:
                        errors.append("malformed hash in pending_transactions")

        # --- who else claims this hash? (the consumed-hash net) -------------
        foreign_scan_complete = True
        if not (isinstance(candidate_hash, str) and _HASH_RE.match(candidate_hash)):
            foreign_scan_complete = False
        if isinstance(candidate_hash, str) and _HASH_RE.match(candidate_hash):
            carriers = self.sql.executions_carrying_hash(candidate_hash)
            if carriers is None:
                foreign_scan_complete = False
                errors.append("could not scan workflow_executions for a foreign binding")
            else:
                for r in carriers:
                    if r.get("id") != execution_id:
                        foreign.append({
                            "where": "workflow_executions.transaction_hashes",
                            "execution_id": r.get("id"),
                            "operation_id": r.get("operation_id"),
                            "workflow_id": r.get("workflow_id"),
                            "status": r.get("status"),
                        })
            rows2 = self.sql.pending_transactions_by_hash(candidate_hash)
            if rows2 is None:
                foreign_scan_complete = False
                errors.append("could not scan pending_transactions for a foreign binding")
            else:
                for r in rows2:
                    if r.get("execution_id") != execution_id:
                        foreign.append({
                            "where": "pending_transactions",
                            "execution_id": r.get("execution_id"),
                            "operation_id": r.get("operation_id"),
                            "nonce": r.get("nonce"),
                            "status": r.get("status"),
                        })

        # --- HTTP cross-check of the id -> execution mapping ----------------
        if self.client is not None:
            try:
                listing = await self.client.executions(self.workflow_id)
                seen = [
                    e.get("id") for e in listing
                    if isinstance(e.get("input"), dict)
                    and e["input"].get("operationId") == operation_id
                ]
                if seen and set(seen) != ({execution_id} if execution_id else set()):
                    return OwnershipFinding(
                        operation_id,
                        available=True,
                        ambiguous=True,
                        execution_id=execution_id,
                        errors=(f"HTTP listing maps this operation to {seen}, SQL to "
                                f"{execution_id!r}; the two records disagree",),
                        detail="ownership ambiguous: KeeperHub's API and database disagree",
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"HTTP executions cross-check failed: {exc}")

        if not live:
            detail = (
                f"KeeperHub has no execution whose recorded input carries operationId "
                f"{operation_id} in workflow {self.workflow_id}. Nothing is bound to this "
                "operation, so no hash can be adopted for it."
            )
        elif owned:
            detail = (
                f"execution {execution_id} is KeeperHub's record for this operation; it "
                f"binds {len({o.hash for o in owned})} distinct hash(es)"
            )
        else:
            detail = (
                f"execution {execution_id} is KeeperHub's record for this operation but "
                "binds no transaction hash in any of the three sources"
            )

        return OwnershipFinding(
            operation_id,
            available=True,
            owned=tuple(owned),
            foreign_bindings=tuple(foreign),
            foreign_scan_complete=foreign_scan_complete,
            errors=tuple(errors),
            detail=detail,
            execution_id=execution_id,
        )


# --------------------------------------------------------------------------
# the verdict
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldCheck:
    field: str
    authorized: Any
    observed: Any
    match: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "authorized": self.authorized,
            "observed": self.observed,
            "match": self.match,
            "note": self.note,
        }


#: Per-process key for the verdict seal. A verdict cannot cross a process, which
#: is intended: crossing one is exactly the "computed earlier or elsewhere" case
#: that apply-time re-verification exists to refuse. See the module docstring for
#: why the seal is defence in depth and not the guarantee.
_SEAL_KEY = secrets.token_bytes(32)


def _sealed_payload(v: "ReconciliationVerdict") -> str:
    """Everything a verdict asserts. Anything omitted here must decide nothing."""
    return json.dumps(
        {
            "gate": GATE_VERSION,
            "operationId": v.operation_id,
            "candidateHash": str(v.candidate_hash).lower(),
            "envelopeDigest": v.envelope_digest,
            "journalKey": v.journal_key,
            "ownershipSources": sorted(v.ownership_sources),
            "ownershipAvailable": v.ownership_available,
            "checks": [[c.field, repr(c.authorized), repr(c.observed), bool(c.match)] for c in v.checks],
            "blockers": list(v.blockers),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _seal_of(v: "ReconciliationVerdict") -> str:
    return hmac.new(_SEAL_KEY, _sealed_payload(v).encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class ReconciliationVerdict:
    """The result of one comparison, sealed to the facts it was computed from.

    Every field the decision depends on is covered by :attr:`seal`. Rebuilding the
    dataclass by hand, ``dataclasses.replace``-ing a field, or mutating a
    ``FieldCheck`` changes the payload and invalidates it, so
    :meth:`authorized_hash` refuses. ``informational`` is deliberately *outside*
    the seal — it carries the receipt and other reporting, and decides nothing.
    """

    operation_id: str
    candidate_hash: str
    envelope_digest: str
    journal_key: str
    checks: tuple[FieldCheck, ...] = ()
    blockers: tuple[str, ...] = ()
    ownership_sources: tuple[str, ...] = ()
    ownership_available: bool = False
    informational: dict[str, Any] = field(default_factory=dict)
    seal: str = ""

    @classmethod
    def issue(cls, **kw: Any) -> "ReconciliationVerdict":
        """The only constructor that produces a valid seal."""
        kw.setdefault("checks", ())
        kw.setdefault("blockers", ())
        kw["checks"] = tuple(kw["checks"])
        kw["blockers"] = tuple(kw["blockers"])
        kw["ownership_sources"] = tuple(kw.get("ownership_sources") or ())
        draft = cls(**kw, seal="")
        return replace(draft, seal=_seal_of(draft))

    @property
    def seal_valid(self) -> bool:
        return bool(self.seal) and hmac.compare_digest(self.seal, _seal_of(self))

    @property
    def binding(self) -> tuple[str, str, str, str]:
        """What this verdict is about. Compared field-for-field before a write."""
        return (
            self.operation_id,
            str(self.candidate_hash).lower(),
            self.envelope_digest,
            self.journal_key,
        )

    @property
    def ownership_established(self) -> bool:
        return bool(self.ownership_available and self.ownership_sources)

    @property
    def ok(self) -> bool:
        return (
            self.seal_valid
            and not self.blockers
            and bool(self.checks)
            and all(c.match for c in self.checks)
            and self.ownership_established
        )

    @property
    def failed_fields(self) -> list[str]:
        return [c.field for c in self.checks if not c.match]

    def reason(self) -> str:
        parts: list[str] = []
        if not self.seal_valid:
            parts.append(
                "this verdict's seal does not match its contents: it was hand-built, "
                "or a field was changed after it was issued, so it attests to nothing"
            )
        if not self.ownership_established:
            parts.append(
                "ownership was not established: no source in KeeperHub's record for "
                "this operation id carries this hash"
            )
        if self.blockers:
            parts.append("blocked: " + "; ".join(self.blockers))
        if self.failed_fields:
            detail = "; ".join(
                f"{c.field}: authorized={c.authorized!r} observed={c.observed!r}"
                for c in self.checks
                if not c.match
            )
            parts.append(f"mismatch on {', '.join(self.failed_fields)} -- {detail}")
        return " | ".join(parts) or "all checks passed"

    def authorized_hash(self) -> str:
        """The ONLY way to obtain a hash for writing. Raises unless every check passed."""
        if not self.ok:
            raise ReconciliationRefused(
                f"refusing to resolve operation {self.operation_id} with "
                f"{self.candidate_hash}: {self.reason()}",
                self,
            )
        return self.candidate_hash

    def as_dict(self) -> dict[str, Any]:
        return {
            "gateVersion": GATE_VERSION,
            "operationId": self.operation_id,
            "candidateHash": self.candidate_hash,
            "envelopeDigest": self.envelope_digest,
            "journalKey": self.journal_key,
            "ok": self.ok,
            "sealValid": self.seal_valid,
            "sealFingerprint": self.seal[:16],
            "ownershipEstablished": self.ownership_established,
            "ownershipSources": list(self.ownership_sources),
            "reason": self.reason(),
            "checks": [c.as_dict() for c in self.checks],
            "blockers": list(self.blockers),
            "informational": self.informational,
            "authorizationSource": "OperationJournal.operations.envelope, written by "
            "journal.begin() before the executor was called",
            "ownershipSource": "KeeperHub's own record for this operation id "
            "(transaction_hashes / node log output / pending_transactions by execution_id)",
            "chainScanIsNotOwnership": True,
            "receiptIsNotACriterion": True,
        }


def _norm_addr(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text.lower() or None


def _norm_hex(value: Any) -> str:
    text = str(value or "0x").strip().lower()
    return text if text.startswith("0x") else "0x" + text


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text, 16) if text.startswith("0x") else int(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# the comparison
# --------------------------------------------------------------------------


async def verify_authorized_operation(
    authorized: AuthorizedOperation,
    candidate_hash: str,
    reader: ChainReader,
    oracle: OwnershipOracle | None,
    *,
    require_pending: bool = True,
) -> ReconciliationVerdict:
    """Compare a candidate transaction with the pre-send authorization.

    Two independent required checks — ownership (KeeperHub's record for *this*
    operation id) and content (the five journalled fields) — plus a consumed-hash
    net. Pure reads: writes nothing, whatever the outcome.

    ``oracle`` is not optional in effect: passing ``None`` produces a blocker, so
    a caller that forgets it gets a refusal, never a pass.
    """
    blockers: list[str] = []
    checks: list[FieldCheck] = []
    info: dict[str, Any] = {}
    ownership_sources: tuple[str, ...] = ()
    ownership_available = False

    def issue() -> ReconciliationVerdict:
        return ReconciliationVerdict.issue(
            operation_id=authorized.operation_id,
            candidate_hash=str(candidate_hash),
            envelope_digest=authorized.computed_digest,
            journal_key=authorized.journal_key,
            checks=checks,
            blockers=blockers,
            ownership_sources=ownership_sources,
            ownership_available=ownership_available,
            informational=info,
        )

    if not isinstance(candidate_hash, str) or not _HASH_RE.match(candidate_hash):
        blockers.append(f"candidate hash {candidate_hash!r} is not a 32-byte 0x hash")
        return issue()

    # Canonicalise once, so the seal, the comparisons and the value written to the
    # journal are all the same bytes the seam's own `record_hash` would store.
    candidate_hash = candidate_hash.lower()

    if authorized.stored_digest != authorized.computed_digest:
        blockers.append(
            f"the journal row's digest column ({authorized.stored_digest[:12]}…) does not "
            f"match the digest of its own envelope ({authorized.computed_digest[:12]}…); "
            "the authorization has been altered since it was written"
        )

    if require_pending and authorized.state != "pending":
        blockers.append(
            f"journal row is {authorized.state!r}, not 'pending'; only an unresolved "
            "operation can be resolved (a resolved one is already recorded, and "
            "re-resolving it would overwrite a settled fact)"
        )

    if authorized.to is None:
        blockers.append(
            "authorized envelope has no `to`; this executor cannot deploy contracts, "
            "so such a row can never be matched"
        )

    # --- check 1: positive ownership binding ---------------------------
    finding: OwnershipFinding | None = None
    if oracle is None:
        blockers.append(
            "no ownership oracle was supplied, so KeeperHub's record for this "
            "operation could not be consulted; content alone can never establish "
            "that a transaction belongs to this operation"
        )
    else:
        try:
            finding = await oracle.ownership(authorized.operation_id, candidate_hash)
        except Exception as exc:  # noqa: BLE001
            blockers.append(f"ownership lookup failed: {exc}")

    if finding is not None:
        ownership_available = bool(finding.available and not finding.ambiguous)
        info["ownership"] = finding.as_dict()
        # Recorded so the evidence says which oracle answered. The gate believes the
        # oracle it is handed (see README §7.5); this at least makes that visible.
        info["ownership"]["oracleClass"] = type(oracle).__name__
        if not finding.available:
            blockers.append(
                "KeeperHub's records could not be read, so ownership is UNKNOWN, not "
                "absent; the operation stays blocked: " + (finding.detail or "no detail")
            )
        elif finding.ambiguous:
            blockers.append("ownership is ambiguous: " + (finding.detail or "no detail"))
        owned = finding.owns(candidate_hash)
        if owned:
            ownership_sources = tuple(finding.sources_for(candidate_hash))
        checks.append(FieldCheck(
            "ownership",
            f"a hash bound to operation {authorized.operation_id} by KeeperHub's own record",
            {
                "recordsReadable": finding.available,
                "executionId": finding.execution_id,
                "boundHashes": sorted({o.hash for o in finding.owned}),
                "matchedSources": list(ownership_sources),
                "hintsIgnored": len(finding.hints),
            },
            owned,
            "identical operations produce identical envelopes by construction, so "
            "content can never establish ownership; only KeeperHub's own record for "
            "THIS operation id can. Chain-scan candidates are hints, never ownership.",
        ))
    else:
        checks.append(FieldCheck(
            "ownership",
            f"a hash bound to operation {authorized.operation_id} by KeeperHub's own record",
            None,
            False,
            "KeeperHub's record for this operation was not consulted at all",
        ))

    # --- check 2: the hash is not already spoken for --------------------
    try:
        journal_bindings = journal_hash_bindings(
            authorized.journal_path, candidate_hash, excluding=authorized.operation_id
        )
        journal_scan_ok = True
    except Exception as exc:  # noqa: BLE001
        journal_bindings = []
        journal_scan_ok = False
        blockers.append(f"could not scan the journal for a previous use of this hash: {exc}")
    foreign = list(finding.foreign_bindings) if finding is not None else []
    # "the scan did not run" is not "the scan found nothing".
    foreign_scan_ok = finding is not None and finding.foreign_scan_complete
    if finding is not None and not finding.foreign_scan_complete:
        blockers.append(
            "KeeperHub could not be scanned for a previous use of this hash, so it is "
            "UNKNOWN whether another operation already claims it"
        )
    all_bindings = journal_bindings + foreign
    checks.append(FieldCheck(
        "hash_not_consumed",
        "no other operation already claims this transaction",
        {"bindings": all_bindings or "none found",
         "journal_scanned": journal_scan_ok,
         "keeperhub_scanned": foreign_scan_ok},
        journal_scan_ok and foreign_scan_ok and not all_bindings,
        "a hash already bound to another operation, here or in KeeperHub's records, "
        "cannot also be this one's. Necessary but NOT sufficient: an old transaction "
        "recorded nowhere still matches five fields, and only ownership refuses it.",
    ))

    # --- the chain reads -------------------------------------------------
    try:
        tx = await reader.transaction(candidate_hash)
    except Exception as exc:  # noqa: BLE001
        blockers.append(f"eth_getTransactionByHash failed: {exc}")
        tx = None
    try:
        rpc_chain = await reader.chain_id()
    except Exception as exc:  # noqa: BLE001
        blockers.append(f"eth_chainId failed: {exc}")
        rpc_chain = None

    if tx is None:
        blockers.append(
            f"the node does not know transaction {candidate_hash}; there is nothing to compare"
        )
        return issue()

    if tx.get("blockNumber") in (None, "0x", ""):
        blockers.append(
            "the transaction is known but not mined; an operator resolves a settled "
            "outcome, not an in-flight one"
        )

    # --- check 3: the five required comparisons -------------------------
    tx_chain = _as_int(tx.get("chainId"))
    chain_observed = {"eth_chainId": rpc_chain, "tx.chainId": tx_chain}
    chain_ok = rpc_chain == authorized.envelope_chain_id and (
        tx_chain is None or tx_chain == authorized.envelope_chain_id
    )
    checks.append(
        FieldCheck(
            "chain_id",
            authorized.envelope_chain_id,
            chain_observed,
            chain_ok,
            "the RPC's own chain id must match, and so must the transaction's "
            "chainId field when it carries one (legacy type-0 transactions do not)",
        )
    )

    checks.append(
        FieldCheck(
            "sender",
            _norm_addr(authorized.envelope_from),
            _norm_addr(tx.get("from")),
            _norm_addr(authorized.envelope_from) == _norm_addr(tx.get("from")),
            "a matching call from a different wallet moves a different position",
        )
    )

    checks.append(
        FieldCheck(
            "recipient",
            _norm_addr(authorized.to),
            _norm_addr(tx.get("to")),
            _norm_addr(authorized.to) == _norm_addr(tx.get("to")),
            "identical calldata to a different market is a different operation",
        )
    )

    auth_data = _norm_hex(authorized.data)
    obs_data = _norm_hex(tx.get("input"))
    checks.append(
        FieldCheck(
            "calldata",
            auth_data,
            obs_data,
            auth_data == obs_data,
            f"exact bytes; authorized {len(auth_data) // 2 - 1}B, "
            f"observed {len(obs_data) // 2 - 1}B",
        )
    )

    obs_value = _as_int(tx.get("value"))
    checks.append(
        FieldCheck(
            "value",
            authorized.value,
            obs_value,
            obs_value is not None and obs_value == authorized.value,
            "wei attached to the call",
        )
    )

    # --- informational only, never a criterion -------------------------
    try:
        receipt = await reader.receipt(candidate_hash)
    except Exception as exc:  # noqa: BLE001
        receipt = None
        info["receipt_error"] = str(exc)
    info["receipt"] = (
        None
        if receipt is None
        else {
            "status": _as_int(receipt.get("status")),
            "blockNumber": _as_int(receipt.get("blockNumber")),
            "gasUsed": _as_int(receipt.get("gasUsed")),
        }
    )
    info["transaction"] = {
        "nonce": _as_int(tx.get("nonce")),
        "blockNumber": _as_int(tx.get("blockNumber")),
        "type": tx.get("type"),
    }
    info["journal_row"] = {
        "state": authorized.state,
        "consumed": authorized.consumed,
        "sender_column": authorized.sender,
        "chain_id_column": authorized.chain_id,
        "recorded_hash": authorized.txn_hash,
        "stored_digest": authorized.stored_digest,
        "computed_digest": authorized.computed_digest,
        "journal": authorized.journal_path,
    }
    info["note"] = (
        "ownership and content are independent and both required. Receipt status is "
        "recorded but is NOT part of the authorization: a reverted transaction that is "
        "owned by this operation and matches all five fields still IS the authorized "
        "operation, and the SDK raises on status==0 when it waits for the receipt."
    )

    return issue()


# --------------------------------------------------------------------------
# the only write
# --------------------------------------------------------------------------


async def mark_resolved(
    journal_path: str | Path,
    verdict: ReconciliationVerdict,
    reader: ChainReader,
    oracle: OwnershipOracle | None,
) -> dict[str, Any]:
    """Record the adopted hash, after re-verifying everything from scratch.

    The verdict handed in is treated as a *claim*, not as authority:

    1. its seal must match its contents, and it must already be ``ok``;
    2. it must have been computed against **this** journal file;
    3. the journal row is re-read, its envelope digest re-derived, and the whole
       comparison — ownership, content, consumed-hash — is re-run **now**;
    4. the fresh verdict must pass and must have the identical binding tuple
       (operation id, hash, envelope digest, journal);
    5. the hash written comes from the *fresh* verdict, and the ``UPDATE`` runs
       inside ``BEGIN IMMEDIATE`` after re-checking the ``pending`` state and that
       no other row holds the hash.

    The row becomes ``submitted`` with ``consumed = 0``, which is exactly the state
    ``resolve_pending`` produces for a ``landed`` lookup (SOURCE executor.py:395-397).
    """
    path = Path(journal_path).resolve()

    claimed = verdict.authorized_hash()  # raises unless sealed AND every check passed

    if verdict.journal_key != journal_key(path):
        raise ReconciliationRefused(
            f"this verdict was computed against a different journal "
            f"(journalKey {verdict.journal_key[:12]}…, this journal is "
            f"{journal_key(path)[:12]}… at {path}); a verdict is not portable",
            verdict,
        )

    fresh_auth = read_authorized_operation(path, verdict.operation_id)
    if fresh_auth.computed_digest != verdict.envelope_digest:
        raise ReconciliationRefused(
            f"operation {verdict.operation_id} now denotes a different envelope "
            f"(verified digest {verdict.envelope_digest[:12]}…, current "
            f"{fresh_auth.computed_digest[:12]}…); refusing to apply a verdict about "
            "an envelope that is no longer the authorization",
            verdict,
        )

    fresh = await verify_authorized_operation(fresh_auth, claimed, reader, oracle)
    txn_hash = fresh.authorized_hash()  # raises unless the RE-RUN check passes

    if fresh.binding != verdict.binding:
        raise ReconciliationRefused(
            f"re-verification produced a different binding than the verdict presented "
            f"({fresh.binding} vs {verdict.binding}); refusing",
            fresh,
        )

    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = conn.execute(
            "SELECT state, txn_hash, consumed, digest, envelope FROM operations"
            " WHERE operation_id=?",
            (verdict.operation_id,),
        ).fetchone()
        if before is None:
            conn.execute("ROLLBACK")
            raise ReconciliationRefused(
                f"operation {verdict.operation_id} vanished from the journal between "
                "verification and write; refusing"
            )
        if before[0] != "pending":
            conn.execute("ROLLBACK")
            raise ReconciliationRefused(
                f"operation {verdict.operation_id} is {before[0]!r} at write time, not "
                "'pending'; refusing to overwrite a settled row"
            )
        if envelope_digest(json.loads(before[4])) != verdict.envelope_digest:
            conn.execute("ROLLBACK")
            raise ReconciliationRefused(
                f"operation {verdict.operation_id}'s envelope changed between "
                "re-verification and write; refusing"
            )
        dupes = conn.execute(
            "SELECT operation_id FROM operations WHERE lower(txn_hash)=lower(?)"
            " AND operation_id<>?",
            (txn_hash, verdict.operation_id),
        ).fetchall()
        if dupes:
            conn.execute("ROLLBACK")
            raise ReconciliationRefused(
                f"{txn_hash} is already recorded for operation(s) "
                f"{[d[0] for d in dupes]} in this journal; a transaction belongs to one "
                "operation",
            )
        cur = conn.execute(
            "UPDATE operations SET state='submitted', txn_hash=?, consumed=0,"
            " updated_at=unixepoch('subsec') WHERE operation_id=? AND state='pending'",
            (txn_hash, verdict.operation_id),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise ReconciliationRefused(
                f"the write for operation {verdict.operation_id} touched "
                f"{cur.rowcount} rows, not 1; refusing"
            )
        after = conn.execute(
            "SELECT state, txn_hash, consumed FROM operations WHERE operation_id=?",
            (verdict.operation_id,),
        ).fetchone()
        conn.execute("COMMIT")
    finally:
        conn.close()
    return {
        "operationId": verdict.operation_id,
        "txnHash": txn_hash,
        "ownershipSources": list(fresh.ownership_sources),
        "ownershipOracle": type(oracle).__name__,
        "reverifiedAtWrite": True,
        "before": {"state": before[0], "txn_hash": before[1], "consumed": bool(before[2])},
        "after": {"state": after[0], "txn_hash": after[1], "consumed": bool(after[2])},
    }


async def resolve_operation(
    journal_path: str | Path,
    operation_id: str,
    candidate_hash: str,
    reader: ChainReader,
    oracle: OwnershipOracle | None,
) -> tuple[ReconciliationVerdict, dict[str, Any]]:
    """Verify then, only if the verdict passes, record. One entry point.

    Verification runs twice on purpose: once here to produce the verdict the
    operator sees, and once inside :func:`mark_resolved` at the moment of the
    write. The second run is the one that decides.
    """
    authorized = read_authorized_operation(journal_path, operation_id)
    verdict = await verify_authorized_operation(authorized, candidate_hash, reader, oracle)
    written = await mark_resolved(journal_path, verdict, reader, oracle)
    return verdict, written
