"""Hosted acceptance wiring; offline exercised, no real hosted recovery witness.

Use build_execution to bind the actual SDK seam and KeeperHubExecutor to a
journal-backed gate. No send is enabled by default. The HTTP ownership check in
KeeperHubExecutor precedes this independent receipt check. Visible agreement is
not a complete hosted history or protection against upstream internal fallback.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from keeperhub_executor.executor import (
    KeeperHubExecutor,
    encode_from_recorded_input,
    ether_string_to_wei,
)
from keeperhub_executor.workflow import ACTION_NODE_ID
from keeperhub_executor.provenance import POLICY_VERSION, trace_witnesses
from wayfinder_paths.core.utils.executor import (
    ExternalExecution,
    OperationJournal,
    ExecutionOutcomeUnknownError,
    build_envelope,
    envelope_digest,
)
from hosted_sepolia.verify import (
    EffectExpectation,
    ExecutionMode,
    MAX_UINT256,
    classify_execution_mode,
    verify_sponsored_effect,
)

CHAIN_ID = 84532
POOL = "0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27"
USDC = "0xba50Cd2A20f6DA35D788639E581bca8d0B5d4D5f"
AUSDC = "0x10F1A9D11CDf50041f3f8cB7191CBE2f31750ACC"
FAUCET = "0xD9145b5F45Ad4519c7ACcD6E0A4A82e83bB8A6Dc"
CALLS = {
    "supply(address,uint256,address,uint16)": (
        "supply",
        POOL,
        ["address", "uint256", "address", "uint16"],
    ),
    "withdraw(address,uint256,address)": (
        "withdraw",
        POOL,
        ["address", "uint256", "address"],
    ),
    "approve(address,uint256)": ("approve", USDC, ["address", "uint256"]),
    "mint(address,address,uint256)": (
        "erc20_mint",
        FAUCET,
        ["address", "address", "uint256"],
    ),
}


def expectation_from_envelope(
    envelope: dict[str, Any], wallet: str
) -> EffectExpectation:
    """Decode only the exact authorized bytes and the pinned testnet targets."""
    env = build_envelope(envelope)
    if (
        env["chainId"] != CHAIN_ID
        or env["from"] != to_checksum_address(wallet)
        or env["value"] != 0
    ):
        raise ValueError("authorized chain/sender/native value outside hosted profile")
    data = bytes.fromhex(env["data"][2:])
    for sig, (kind, target, types) in CALLS.items():
        if data[:4] != function_signature_to_4byte_selector(sig):
            continue
        if env["to"] != to_checksum_address(target):
            raise ValueError("authorized selector at wrong target")
        args = decode(types, data[4:])
        if encode(types, args) != data[4:]:
            raise ValueError("authorized calldata does not round-trip exactly")
        kw = dict(kind=kind, chain_id=CHAIN_ID, target=target, wallet=wallet)
        if kind in {"supply", "withdraw"}:
            if to_checksum_address(args[0]) != to_checksum_address(
                USDC
            ) or to_checksum_address(args[2]) != to_checksum_address(wallet):
                raise ValueError("wrong reserve or position owner/recipient")
            kw.update(
                asset=USDC,
                amount=args[1],
                recipient=wallet,
                a_token=AUSDC,
                exact_amount=not (kind == "withdraw" and args[1] == MAX_UINT256),
            )
            if kind == "supply":
                kw["referral_code"] = args[3]
        elif kind == "approve":
            if to_checksum_address(args[0]) != to_checksum_address(POOL):
                raise ValueError("wrong allowance spender")
            kw.update(spender=POOL, amount=args[1])
        else:
            if to_checksum_address(args[0]) != to_checksum_address(
                USDC
            ) or to_checksum_address(args[1]) != to_checksum_address(wallet):
                raise ValueError("wrong mint token or recipient")
            kw.update(asset=USDC, amount=args[2])
        return EffectExpectation(**kw)
    raise ValueError("unsupported authorized selector")


class VerifiedJournal(OperationJournal):
    """Store evidence semantics with the hash; reject unchecked SDK transitions.

    Single writer, as the underlying SDK seam assumes. SQL checks run in the
    same transaction as record_hash and re-read the authorization. This is not
    remote fencing, cross-process send locking, or proof against DB tampering.
    """

    def __init__(self, path):
        super().__init__(path)
        self._attempt = None
        self._permission = None
        self._conn.execute("""CREATE TABLE IF NOT EXISTS hosted_verification (
            operation_id TEXT PRIMARY KEY, txn_hash TEXT NOT NULL,
            digest TEXT NOT NULL, proof TEXT NOT NULL)""")

    def observations(self, operation_id):
        self._conn.execute("CREATE TABLE IF NOT EXISTS hosted_observations (operation_id TEXT, txn_hash TEXT, observation TEXT NOT NULL, PRIMARY KEY(operation_id,txn_hash))")
        return [json.loads(row[0]) for row in self._conn.execute(
            "SELECT observation FROM hosted_observations WHERE operation_id=?", (operation_id,))]

    def save_observation(self, operation_id, txn_hash, observation):
        self.observations(operation_id)
        # Legacy proofs lack the accepted observation. Preserve it before the
        # mutable latest-observation slot is replaced. Called inside finish's
        # short transaction, so a failed save cannot split these writes.
        previous = self._conn.execute(
            "SELECT observation FROM hosted_observations WHERE operation_id=? AND txn_hash=?",
            (operation_id, txn_hash),
        ).fetchone()
        stored = self._proof_row(operation_id)
        if previous and stored and stored[0] == txn_hash:
            old = json.loads(previous[0])
            proof = json.loads(stored[2])
            if (old.get("accepted") is True and proof.get("ok") is True
                    and old.get("authorizationDigest") == stored[1]
                    and "acceptedObservation" not in proof):
                proof["acceptedObservation"] = old
                self.save_proof(operation_id, txn_hash, stored[1], proof)
        self._conn.execute("INSERT OR REPLACE INTO hosted_observations VALUES (?,?,?)",
                           (operation_id, txn_hash, json.dumps(observation, sort_keys=True)))

    def invalidate_validation(self):
        """Forget authority, never evidence. Also supersede suspended attempts."""
        self._attempt = None
        self._permission = None

    def start_verification(self):
        self.invalidate_validation()
        self._attempt = object()
        return self._attempt

    def _proof_row(self, operation_id):
        return self._conn.execute(
            "SELECT txn_hash,digest,proof FROM hosted_verification WHERE operation_id=?",
            (operation_id,),
        ).fetchone()

    def finish_verification(self, attempt, row, txn_hash, proof, observation):
        """Persist evidence atomically, then grant process-local single-use authority.

        No await here. A later attempt supersedes a suspended earlier verifier.
        The permission is a snapshot, not a timestamp or a durable ok flag.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if attempt is not self._attempt or self.authorized(row["operation_id"]) != row:
                raise ValueError("verification attempt or authorization changed")
            self.save_observation(row["operation_id"], txn_hash, observation)
            if proof["ok"]:
                stored = {**proof, "acceptedObservation": observation}
                self.save_proof(row["operation_id"], txn_hash, row["digest"], stored)
            saved = self._proof_row(row["operation_id"])
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        if proof["ok"]:
            self._permission = (attempt, deepcopy(row), txn_hash, saved)

    def _take_permission(self, permission, operation_id, txn_hash, envelope=None):
        row = self.authorized(operation_id)
        if (permission is None or permission[0] is not self._attempt
                or permission[1] != row or permission[2] != txn_hash
                or permission[3] != self._proof_row(operation_id)
                or (envelope is not None and envelope != row["envelope"])):
            raise ValueError("no matching fresh verification permission for journal transition")
        if row["consumed"]:
            raise ValueError("operation already consumed; no journal transition")
        return row

    def authorized(self, operation_id):
        rows = [r for r in self.entries() if r["operation_id"] == operation_id]
        if len(rows) != 1:
            raise ValueError("operation absent from SDK journal")
        row = rows[0]
        env = row["envelope"]
        if (
            row["digest"] != envelope_digest(env)
            or env != build_envelope(env)
            or row["sender"] != env["from"].lower()
            or row["chain_id"] != env["chainId"]
        ):
            raise ValueError("SDK journal authorization is inconsistent")
        if row["state"] not in {"pending", "submitted"}:
            raise ValueError("operation not pending/submitted")
        return row

    def save_proof(self, operation_id, txn_hash, digest, proof):
        self._conn.execute(
            "INSERT OR REPLACE INTO hosted_verification VALUES (?,?,?,?)",
            (operation_id, txn_hash, digest, json.dumps(proof, sort_keys=True)),
        )

    def record_hash(self, operation_id, txn_hash, *, consumed):
        permission, self._permission = self._permission, None
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._take_permission(permission, operation_id, txn_hash)
            if row["txn_hash"] is not None and row["txn_hash"] != txn_hash:
                raise ValueError("journal hash conflict")
            if any(
                r["operation_id"] != operation_id and r["txn_hash"] == txn_hash
                for r in self.entries()
            ):
                raise ValueError("hash already bound to another SDK operation")
            super().record_hash(operation_id, txn_hash, consumed=consumed)
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def consume_claim(self, envelope, operation_id, txn_hash):
        permission, self._permission = self._permission, None
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._take_permission(permission, operation_id, txn_hash, envelope)
            if self.claim(envelope) != (operation_id, txn_hash):
                raise ValueError("claim is no longer the latest eligible operation")
            if any(r["operation_id"] != operation_id and r["txn_hash"] == txn_hash
                   for r in self.entries()):
                raise ValueError("hash already bound to another SDK operation")
            super().consume_claim(envelope, operation_id, txn_hash)
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def mark_never_seen(self, operation_id):
        raise ValueError("hosted HTTP does not establish never_seen")


class HostedVerificationGate:
    def __init__(
        self,
        journal: VerifiedJournal,
        wallet: str,
        workflow_id: str,
        *,
        allow_submit=False,
    ):
        self.journal = journal
        self.wallet = to_checksum_address(wallet)
        self.allow_submit = allow_submit
        self.workflow_id = workflow_id
        self._scope = ContextVar("hosted_verification_attempt", default=None)

    def before_submit(self, operation_id, envelope):
        if not self.allow_submit:
            raise ValueError("submit disabled: local preparation only")
        row = self.journal.authorized(operation_id)
        if (
            row["state"] != "pending"
            or row["txn_hash"] is not None
            or row["envelope"] != envelope
        ):
            raise ValueError("submit differs from durable pending authorization")
        expectation_from_envelope(row["envelope"], self.wallet)

    @contextmanager
    def attempt(self):
        attempt = self.journal.start_verification()
        context = self._scope.set(attempt)
        try:
            yield
        except BaseException:
            if self.journal._attempt is attempt:
                self.journal.invalidate_validation()
            raise
        finally:
            self._scope.reset(context)

    async def verify(self, operation_id, txn_hash, record, tx, logs, rpc, binding):
        # Direct verifier callers get a new attempt too. Executor lookup enters
        # this scope before any HTTP/ownership checks, including early refusals.
        if self._scope.get() is None:
            with self.attempt():
                return await self._verify(operation_id, txn_hash, record, tx, logs, rpc, binding)
        return await self._verify(operation_id, txn_hash, record, tx, logs, rpc, binding)

    async def _verify(self, operation_id, txn_hash, record, tx, logs, rpc, binding):
        attempt = self._scope.get()
        try:
            if (
                binding.get("operationId") != operation_id
                or binding.get("workflowId") != self.workflow_id
                or binding.get("nodeId") != ACTION_NODE_ID
                or binding.get("chainId") != CHAIN_ID
                or binding.get("status")
                not in {"success", "error", "system_error", "cancelled"}
                or not binding.get("hashReferences")
                or any(h != txn_hash for _, h, _ in binding["hashReferences"])
            ):
                raise ValueError("invalid executor ownership binding")
            row = self.journal.authorized(operation_id)
            env = row["envelope"]
            if row["txn_hash"] and row["txn_hash"] != txn_hash:
                raise ValueError("candidate conflicts with SDK journal hash")
            if any(
                r["operation_id"] != operation_id and r["txn_hash"] == txn_hash
                for r in self.journal.entries()
            ):
                raise ValueError("candidate hash used by another operation")
            want = expectation_from_envelope(env, self.wallet)
            # KeeperHub data is compared to authorization, never used to create it.
            if (
                record.get("operationId") != operation_id
                or str(record.get("network")) != str(env["chainId"])
                or str(record.get("contractAddress", "")).lower() != env["to"].lower()
                or encode_from_recorded_input(record) != env["data"]
                or ether_string_to_wei(record.get("ethValue", "0")) != env["value"]
            ):
                raise ValueError(
                    "KeeperHub recorded input differs from SDK authorization"
                )
            action = [lg for lg in logs if lg.get("nodeType") == "web3/write-contract"]
            if len(action) != 1:
                raise ValueError("ambiguous action logs")
            lg = action[0]
            if (
                lg.get("executionId") != binding["executionId"]
                or lg.get("nodeId") != ACTION_NODE_ID
            ) or lg.get("status") not in {
                "success",
                "error",
                "cancelled",
            }:
                raise ValueError("mode-bearing action has wrong identity or lifecycle")
            out = lg.get("output")
            raw = lg.get("outputRaw")
            mode = classify_execution_mode(out, raw)
            if mode.mode is not ExecutionMode.SPONSORED:
                raise ValueError("sponsorship not established: " + mode.detail)
            receipt = await rpc("eth_getTransactionReceipt", [txn_hash])
            chain = int(await rpc("eth_chainId", []), 16)
            try:
                traces, redactions = trace_witnesses(lg)
            except ValueError as exc:
                # Ownership and local authorization were checked above. Retain
                # this attributed rejection through the common observation path,
                # including the authorization recheck after the RPC awaits.
                # A rejected trace is not an absent trace: do not run the optional
                # trace branch of the effect verifier for this candidate.
                redactions = []
                results = [{
                    "ok": False,
                    "profile": "sponsored",
                    "stage": "trace_derivation",
                    "source": "KeeperHub node log output/outputRaw",
                    "blockers": [str(exc)],
                    "checks": [],
                    "evidence_origins": ["KeeperHub"],
                    "caveats": ["Trace derivation failed; effect verification was not run."],
                }]
            else:
                # Each derivative was checked against its linked full witness.
                results = [
                    verify_sponsored_effect(
                        tx,
                        receipt,
                        want,
                        rpc_chain_id=chain,
                        txn_hash=txn_hash,
                        wallet_address=self.wallet,
                        executed_call=ec,
                    ).as_dict()
                    for ec in (traces or [None])
                ]
            proof = {
                "ok": all(r["ok"] for r in results),
                "operationId": operation_id,
                "txnHash": txn_hash,
                "authorizationDigest": row["digest"],
                "mode": mode.mode.value,
                "modeSources": mode.sources,
                "ownership": binding,
                "policyVersion": POLICY_VERSION,
                "provenance": binding.get("provenance"),
                "redactions": redactions,
                "results": results,
                "goalVerified": False,
                "meaning": "согласованные наблюдения записей KeeperHub плюс отдельная проверка эффекта по цепи",
                "limitations": "effect in transaction; not exclusive calldata, complete history, or strategy goal",
                "blockers": [b for r in results for b in r["blockers"]],
            }
            # Re-read after awaits; the SDK transition rechecks again in a SQL transaction.
            if self.journal.authorized(operation_id) != row:
                raise ValueError("authorization changed during verification")
            # Retain attributed failed candidates, including a reverted receipt,
            # without turning them into accepted hashes or successful effects.
            observation = {
                "operationId": operation_id, "txnHash": txn_hash,
                "authorizationDigest": row["digest"], "ownership": binding,
                "receipt": receipt, "verification": proof, "accepted": proof["ok"],
            }
            self.journal.finish_verification(attempt, row, txn_hash, proof, observation)
            return proof
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return {"ok": False, "blockers": [str(exc)]}


class HostedExternalExecution(ExternalExecution):
    """Resume only the interrupted envelope; never replay earlier strategy steps."""

    async def send(self, transaction):
        self.journal.invalidate_validation()
        env = build_envelope(transaction)
        for row in self.journal.entries():
            if (
                row["sender"] != self.wallet_address.lower()
                or row["chain_id"] != env["chainId"]
            ):
                continue
            if row["state"] == "orphaned":
                raise ExecutionOutcomeUnknownError(
                    row["operation_id"],
                    "orphaned operation needs driver reconciliation",
                )
            if row["state"] == "pending" or (
                row["state"] == "submitted" and not row["consumed"]
            ):
                if row["envelope"] != env:
                    raise ExecutionOutcomeUnknownError(
                        row["operation_id"],
                        "resume must start at the interrupted operation",
                    )
                if row["state"] == "submitted":
                    result = await self.executor.lookup(row["operation_id"])
                    if (
                        result.outcome.value != "landed"
                        or result.txn_hash != row["txn_hash"]
                    ):
                        raise ExecutionOutcomeUnknownError(
                            row["operation_id"],
                            "unconsumed hash failed re-verification",
                        )
        return await super().send(transaction)


def build_execution(
    *,
    journal_path,
    client,
    workflow_id,
    wallet_address,
    rpc_url,
    abi_resolver,
    allow_submit=False,
    **executor_options,
):
    """Explicit wiring, no workflow creation/funding/reset/broadcast on construction."""
    journal = VerifiedJournal(journal_path)
    gate = HostedVerificationGate(
        journal, wallet_address, workflow_id, allow_submit=allow_submit
    )
    executor = KeeperHubExecutor(
        execution_profile="sponsored",
        client=client,
        workflow_id=workflow_id,
        wallet_address=wallet_address,
        chain_id=CHAIN_ID,
        rpc_url=rpc_url,
        abi_resolver=abi_resolver,
        verification_gate=gate,
        **executor_options,
    )
    return HostedExternalExecution(executor, journal)
