"""KeeperHub-backed executor for the pinned Wayfinder external-executor seam.

An operation-bound, consistent hash may be adopted after a known terminal run.
The SDK still verifies its receipt; adoption is not transaction success or
irreversible nonce consumption. Calldata scans are diagnostic only: identical
transactions do not identify operations, and missing candidates prove no absence.
No current KeeperHub read supplies durable request quiescence. Consequently this
implementation never grants the SDK permission to resend via ``never_seen``.
Even an empty database or a pre-sign error log cannot fence a delayed old POST.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from eth_abi import encode as abi_encode
from eth_utils import to_checksum_address

from wayfinder_paths.core.utils.executor import LookupResult

from .authorization import LocalAuthorization
from .provenance import record_provenance
from .abis import AbiResolver, default_resolver
from .calldata import (
    CalldataReproductionError,
    DecodedCall,
    canonical_type,
    decode_and_verify,
    find_function_by_selector,
)
from .client import EXECUTIONS_PAGE_CAP, ExecuteOutcome, KeeperHubClient
from .reconcile import ChainReconciler, KeeperHubDbProbe
from .workflow import ACTION_NODE_ID, build_workflow_definition

WEI_PER_ETHER = 10**18
# Pinned upstream lib/errors/execution-status.ts and x402/execution-wait.ts.
TERMINAL_STATUSES = {"success", "error", "system_error", "cancelled", "skipped"}
IN_FLIGHT_STATUSES = {"pending", "running", "unconfirmed"}
# `phantom` is declared upstream but does not establish terminal execution.
NODE_STATUSES = {"pending", "running", "success", "error", "cancelled"}


def ether_string_to_wei(value: str | int) -> int:
    """Parse nonnegative decimal ether without binary float or Decimal context.

    Match parseEther's decimal precision (extra trailing zeroes are harmless),
    bound the EVM value to uint256, and reject scientific notation and floats.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("ethValue must be a decimal string or integer")
    text = str(value)
    if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", text):
        raise ValueError("ethValue must be a nonnegative decimal without exponent")
    whole, _, fraction = text.partition(".")
    fraction = fraction.rstrip("0")
    if len(fraction) > 18:
        raise ValueError("ethValue has fractional wei")
    result = int(whole or "0") * WEI_PER_ETHER + int(fraction.ljust(18, "0") or "0")
    if result > 2**256 - 1:
        raise ValueError("ethValue exceeds uint256")
    return result


class KeeperHubSubmissionRefused(RuntimeError):
    """Current request refused; does not prove an older request never signed.

    Retained for API compatibility; current lookup never grants never_seen.
    """


def wei_to_ether_string(wei: int) -> str:
    """Exact wei -> decimal-ether string, the only shape `ethValue` accepts.

    ``write-contract-core.ts:456-458`` runs ``ethers.parseEther(ethValue)``, so
    the field is *ether*, not wei. The inverse is checked before returning: an
    envelope whose value cannot be expressed losslessly is refused rather than
    rounded.
    """
    wei = int(wei)
    if wei < 0 or wei > 2**256 - 1:
        raise ValueError(f"value outside uint256: {wei}")
    whole, frac = divmod(wei, WEI_PER_ETHER)
    text = str(whole) if frac == 0 else f"{whole}.{frac:018d}".rstrip("0")
    # parseEther's inverse, computed the same way ethers does it.
    if "." in text:
        ip, fp = text.split(".")
        check = int(ip) * WEI_PER_ETHER + int(fp.ljust(18, "0"))
    else:
        check = int(text) * WEI_PER_ETHER
    if check != wei:
        raise ValueError(f"value {wei} wei does not round-trip through parseEther")
    return text


def encode_from_recorded_input(record: dict[str, Any]) -> str | None:
    """Rebuild the calldata a run submitted, from KeeperHub's own `input` column.

    This is what lets the chain reconciler run with no executor-local state: the
    execution row stores the ABI, the function name and the args that were sent,
    so the exact bytes can be recomputed from KeeperHub's records alone.
    """
    try:
        abi = record["abi"]
        abi = json.loads(abi) if isinstance(abi, str) else abi
        args = record["functionArgs"]
        args = json.loads(args) if isinstance(args, str) else args
        name = record["abiFunction"]
    except Exception:  # noqa: BLE001
        return None
    from .calldata import _from_json_value, selector_of  # noqa: PLC0415

    fn = None
    for entry in abi:
        if entry.get("type") == "function" and entry.get("name") == name:
            fn = entry
            break
    if fn is None:
        return None
    try:
        types = [canonical_type(i) for i in fn.get("inputs", [])]
        values = [
            _from_json_value(v, i["type"], i.get("components"))
            for v, i in zip(args, fn.get("inputs", []), strict=True)
        ]
        return selector_of(fn) + abi_encode(types, values).hex()
    except Exception:  # noqa: BLE001
        return None


@dataclass
class LookupEvidence:
    """Everything `lookup` looked at, saved verbatim next to its answer."""

    operation_id: str
    outcome: str = ""
    detail: str = ""
    txn_hash: str | None = None
    executions_returned: int | None = None
    executions_truncated: bool = False
    execution: dict[str, Any] | None = None
    logs_execution: dict[str, Any] | None = None
    node_logs: list[dict[str, Any]] = field(default_factory=list)
    idempotency_record: dict[str, Any] | None = None
    idempotency_probe_available: bool = False
    pending_transactions: list[dict[str, Any]] | None = None
    chain_scan: dict[str, Any] | None = None
    chain_matches: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    verification: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "operationId": self.operation_id,
            "outcome": self.outcome,
            "detail": self.detail,
            "txnHash": self.txn_hash,
            "executionsReturned": self.executions_returned,
            "executionsTruncated": self.executions_truncated,
            "execution": self.execution,
            "logsExecution": self.logs_execution,
            "nodeLogs": self.node_logs,
            "idempotencyRecord": self.idempotency_record,
            "idempotencyProbeAvailable": self.idempotency_probe_available,
            "pendingTransactions": self.pending_transactions,
            "chainScan": self.chain_scan,
            "chainMatches": self.chain_matches,
            "errors": self.errors,
            "verification": self.verification,
            "provenance": self.provenance,
        }


class OperationVerificationGate(Protocol):
    """Sponsored effect gate, layered on mandatory local SDK authorization.

    Existing direct validation remains the default. A configured gate owns the
    content decision after the executor has checked all visible hash bindings.
    Returning evidence is required before submit/lookup may return a landed hash.
    """

    def before_submit(self, operation_id: str, envelope: dict[str, Any]) -> None: ...

    async def verify(self, operation_id: str, txn_hash: str, record: dict[str, Any],
                     tx: dict[str, Any] | None, logs: list[dict[str, Any]],
                     rpc: Callable, binding: dict[str, Any]) -> dict[str, Any]: ...


class KeeperHubExecutor:
    """Submit Wayfinder envelopes through KeeperHub; answer lookups from its records."""

    def __init__(
        self,
        *,
        client: KeeperHubClient,
        workflow_id: str,
        wallet_address: str,
        chain_id: int,
        rpc_url: str,
        abi_resolver: AbiResolver = default_resolver,
        db_probe: KeeperHubDbProbe | None = None,
        require_db_probe: bool = True,
        submit_timeout: float = 180.0,
        poll_interval: float = 1.0,
        crash_hook: Callable[[str, dict[str, Any]], None] | None = None,
        on_evidence: Callable[[str, dict[str, Any]], None] | None = None,
        verification_gate: OperationVerificationGate | None = None,
        execution_profile: str | None = None,
    ) -> None:
        self.client = client
        self.workflow_id = workflow_id
        self.wallet_address = to_checksum_address(wallet_address)
        self.chain_id = int(chain_id)
        self.abi_resolver = abi_resolver
        self.reconciler = ChainReconciler(rpc_url)
        self.db_probe = db_probe
        # Retain the keyword for old callers, but False cannot weaken safety.
        self.require_db_probe = True
        self.submit_timeout = submit_timeout
        self.poll_interval = poll_interval
        self.crash_hook = crash_hook
        self.on_evidence = on_evidence
        self.submits = 0
        self.verification_gate = verification_gate
        self.execution_profile = execution_profile
        self.authorization = None

    def bind_journal(self, journal):
        if self.authorization is not None and self.authorization.journal is not journal:
            raise ValueError("executor already bound to another SDK journal")
        if self.verification_gate is not None and self.verification_gate.journal is not journal:
            raise ValueError("profile gate and SDK use different journals")
        self.authorization = LocalAuthorization(journal, self.wallet_address, self.chain_id)

    async def validate_result(self, operation_id, txn_hash):
        try:
            result = await self.lookup(operation_id)
            if result.outcome.value != "landed" or result.txn_hash != txn_hash:
                from wayfinder_paths.core.utils.executor import ExecutionOutcomeUnknownError
                raise ExecutionOutcomeUnknownError(operation_id, result.detail or "hash validation failed")
        except BaseException:
            if self.authorization is not None:
                self.authorization.journal.invalidate_validation()
            raise


    # -- lifecycle ---------------------------------------------------------

    @classmethod
    async def create_workflow(
        cls,
        client: KeeperHubClient,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        return await client.create_workflow(build_workflow_definition(name, description))

    async def close(self) -> None:
        await self.reconciler.close()

    @property
    def idempotency_scope(self) -> str:
        return f"workflow-execute:{self.workflow_id}"

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_evidence:
            self.on_evidence(kind, payload)

    def _crash(self, stage: str, payload: dict[str, Any]) -> None:
        if self.crash_hook:
            self.crash_hook(stage, payload)

    # -- calldata -> write-contract ---------------------------------------

    def prepare(self, envelope: dict[str, Any]) -> tuple[DecodedCall, dict[str, Any]]:
        """Decode + byte-verify, then build the execute payload. Refuses on mismatch."""
        if int(envelope["chainId"]) != self.chain_id:
            raise ValueError(
                f"envelope chainId {envelope['chainId']} != executor chain {self.chain_id}"
            )
        if to_checksum_address(envelope["from"]) != self.wallet_address:
            raise ValueError(
                f"envelope from {envelope['from']} != executor address {self.wallet_address}"
            )
        to = envelope.get("to")
        if not to:
            raise CalldataReproductionError(
                "envelope has no `to`: KeeperHub's write-contract node addresses a "
                "contract, it cannot deploy one"
            )
        data = envelope.get("data") or "0x"
        if len(data) < 10:
            raise CalldataReproductionError(
                f"calldata {data!r} carries no function selector; write-contract "
                "requires ABI + functionName + args"
            )
        selector = data[:10]
        abi = self.abi_resolver(self.chain_id, to, selector)
        if abi is None:
            raise CalldataReproductionError(
                f"no ABI available for {to} on chain {self.chain_id} declaring "
                f"selector {selector}"
            )
        if find_function_by_selector(abi, selector) is None:
            raise CalldataReproductionError(
                f"resolved ABI for {to} does not declare selector {selector}"
            )

        decoded = decode_and_verify(data, abi)  # raises unless byte-equal

        payload = {
            "operationId": "",  # filled by submit
            "network": str(self.chain_id),
            "contractAddress": to_checksum_address(to),
            "abi": decoded.abi,
            "abiFunction": decoded.function_name,
            "functionArgs": decoded.args,
            "ethValue": wei_to_ether_string(int(envelope.get("value") or 0)),
        }
        return decoded, payload

    # -- TransactionExecutor ----------------------------------------------

    async def submit(self, envelope: dict[str, Any], *, operation_id: str) -> str:
        if self.authorization is not None:
            self.authorization.journal.invalidate_validation()
        if self.authorization is None:
            raise ValueError("SDK journal must be bound before submit")
        if self.execution_profile not in {"direct", "sponsored"}:
            raise ValueError("explicit execution profile required")
        self.authorization.before_submit(operation_id, envelope)
        if self.verification_gate is not None:
            self.verification_gate.before_submit(operation_id, envelope)
        decoded, payload = self.prepare(envelope)
        payload["operationId"] = operation_id
        self.submits += 1
        self._emit(
            "prepare",
            {
                "operationId": operation_id,
                "envelope": {**envelope, "value": int(envelope.get("value") or 0)},
                "decoded": decoded.as_evidence(),
                "executePayload": payload,
            },
        )

        outcome: ExecuteOutcome = await self.client.execute(
            self.workflow_id, payload, idempotency_key=operation_id
        )
        self._emit(
            "execute",
            {
                "operationId": operation_id,
                "kind": outcome.kind,
                "status": outcome.status,
                "body": outcome.body,
                "executionId": outcome.execution_id,
            },
        )
        self._crash("after_execute_post", {"operationId": operation_id, "outcome": outcome.kind})

        if outcome.kind == "conflict":
            # The key is bound to a different body. Never rotate: the original
            # request may already have broadcast. Raising leaves the SDK's row
            # pending, and the next lookup finds the original execution.
            raise RuntimeError(
                f"KeeperHub idempotency conflict for operation {operation_id}: the key "
                f"is bound to a different payload (originalExecutionId="
                f"{outcome.execution_id}). Not resubmitting."
            )

        deadline = time.monotonic() + self.submit_timeout
        last: LookupEvidence | None = None
        while time.monotonic() < deadline:
            result, ev = await self._lookup_with_evidence(operation_id)
            last = ev
            if result.outcome.value == "landed":
                # Submit's poll is evidence only; the SDK validates again before
                # accepting a returned hash, including after a response loss.
                self.authorization.journal.invalidate_validation()
                self._crash(
                    "after_hash",
                    {"operationId": operation_id, "txnHash": result.txn_hash},
                )
                return result.txn_hash  # type: ignore[return-value]
            if result.outcome.value == "never_seen":
                raise KeeperHubSubmissionRefused(
                    f"KeeperHub asserts operation {operation_id} was never signed: "
                    f"{result.detail}"
                )
            await asyncio.sleep(self.poll_interval)

        raise RuntimeError(
            f"KeeperHub did not report a transaction hash for operation {operation_id} "
            f"within {self.submit_timeout}s (last: {last.detail if last else 'no lookup'}). "
            "The operation may be on chain; the SDK must not resend."
        )

    async def lookup(self, operation_id: str) -> LookupResult:
        try:
            result, evidence = await self._lookup_with_evidence(operation_id)
            self._emit("lookup", evidence.as_dict())
            return result
        except BaseException:
            if self.authorization is not None:
                self.authorization.journal.invalidate_validation()
            raise

    # -- the actual decision ----------------------------------------------

    async def _lookup_with_evidence(
        self, operation_id: str
    ) -> tuple[LookupResult, LookupEvidence]:
        if self.verification_gate is not None and callable(getattr(self.verification_gate, "attempt", None)):
            with self.verification_gate.attempt():
                return await self._lookup_attempt(operation_id)
        if self.authorization is not None:
            self.authorization.journal.invalidate_validation()
        return await self._lookup_attempt(operation_id)

    async def _lookup_attempt(self, operation_id):
        ev = LookupEvidence(operation_id=operation_id)

        try:
            executions = await self.client.executions(self.workflow_id)
        except Exception as exc:  # noqa: BLE001
            ev.errors.append(f"executions listing failed: {exc}")
            return self._answer(
                ev,
                LookupResult.indeterminate(
                    "KeeperHub's executions API is unreachable, so nothing about "
                    f"operation {operation_id} can be asserted: {exc}"
                ),
            )

        ev.executions_returned = len(executions)
        ev.executions_truncated = len(executions) >= EXECUTIONS_PAGE_CAP

        matches = [
            e
            for e in executions
            if isinstance(e.get("input"), dict)
            and e["input"].get("operationId") == operation_id
        ]

        if len(matches) > 1:
            ev.execution = matches[0]
            return self._answer(
                ev,
                LookupResult.indeterminate(
                    f"{len(matches)} executions carry operationId {operation_id}; "
                    "the idempotency key should have made that impossible, so the "
                    "outcome cannot be attributed to one run"
                ),
            )

        if not matches:
            return await self._no_execution(operation_id, ev)

        return await self._with_execution(operation_id, matches[0], ev)

    async def _no_execution(
        self, operation_id: str, ev: LookupEvidence
    ) -> tuple[LookupResult, LookupEvidence]:
        """Absence is observation, never durable permission to resend."""
        if self.db_probe is not None:
            try:
                record = await self.db_probe.record(scope=self.idempotency_scope, key=operation_id)
                ev.idempotency_probe_available = record is not None
                ev.idempotency_record = record
            except Exception as exc:
                ev.errors.append(f"idempotency probe failed: {exc}")
        return self._answer(ev, LookupResult.indeterminate(
            "No visible execution binds this operation. Neither a short/empty listing "
            "nor an empty idempotency table fences a delayed POST or proves complete "
            "history. Request quiescence is not established; do not resend."
        ))

    async def _with_execution(
        self, operation_id: str, execution: dict[str, Any], ev: LookupEvidence
    ) -> tuple[LookupResult, LookupEvidence]:
        ev.execution = execution
        exec_id = execution.get("id")
        status = execution.get("status")
        record = execution.get("input") or {}

        def unknown(detail: str):
            return self._answer(ev, LookupResult.indeterminate(detail))

        if not isinstance(exec_id, str) or not exec_id or execution.get("workflowId") != self.workflow_id:
            return unknown("Invalid execution/workflow provenance")
        def envelope_identity(value: Any) -> tuple[str, str, int]:
            if not isinstance(value, dict) or value.get("operationId") != operation_id:
                raise ValueError("Wrong operation provenance")
            if str(value.get("network")) != str(self.chain_id):
                raise ValueError("Wrong chain provenance")
            target = to_checksum_address(value["contractAddress"])
            amount = ether_string_to_wei(value.get("ethValue", "0"))
            calldata = encode_from_recorded_input(value)
            if calldata is None:
                raise ValueError("Recorded input cannot be re-encoded")
            return target, calldata.lower(), amount

        try:
            recorded_envelope = envelope_identity(record)
        except (TypeError, ValueError, KeyError, AttributeError):
            return unknown("Malformed recorded execution envelope or operation/chain provenance")
        # SQS may reclaim these never-dispatched infrastructure failures.
        if status == "system_error" and execution.get("errorCode") in {"P-0001", "P-0005"}:
            return unknown("Dispatch-reclaimable system_error is not settled; do not adopt or resend")
        if status not in TERMINAL_STATUSES | IN_FLIGHT_STATUSES:
            return unknown(f"Unrecognized or non-terminal lifecycle status {status!r}; do not adopt")

        found: list[tuple[str, str, str]] = []
        provenance_errors: list[str] = []

        def add(source: str, value: Any, note: str):
            if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
                provenance_errors.append(f"Malformed hash in {source}")
            else:
                found.append((source, value.lower(), note))

        def collect_execution_hashes(row: dict[str, Any], source: str):
            # Reconciliation may retain hashes even on error. Empty/null is not
            # proof of absence; malformed records cannot supply trusted hashes.
            hashes = row.get("transactionHashes")
            if hashes is None:
                hashes = []
            if not isinstance(hashes, list):
                provenance_errors.append(f"Malformed transactionHashes in {source}")
                return
            for entry in hashes:
                if (not isinstance(entry, dict) or entry.get("nodeId") != ACTION_NODE_ID
                    or entry.get("chainId") != self.chain_id):
                    provenance_errors.append(f"Wrong node/chain in {source}")
                    continue
                add(source, entry.get("hash"),
                    f"verified={entry.get('verified')} receiptStatus={entry.get('receiptStatus')}")

        collect_execution_hashes(execution, "workflow_executions.transaction_hashes")

        try:
            payload = await self.client.execution_logs(exec_id)
            logs = payload.get("logs")
            if not isinstance(logs, list):
                raise ValueError("logs is not a list")
        except Exception as exc:
            ev.errors.append(f"execution logs unavailable: {exc}")
            return unknown("Execution logs unavailable; conflicting sources cannot be checked")
        ev.node_logs = logs
        # This route returns a second execution snapshot alongside its logs.
        # Validate both observations and union hashes; never prefer the older
        # listing when the subsequent HTTP response already contradicts it.
        fresh = payload.get("execution")
        if not isinstance(fresh, dict):
            return unknown("Logs response has no execution object; provenance unchecked")
        ev.logs_execution = fresh
        ev.provenance = record_provenance(execution, fresh, logs)
        if fresh.get("id") != exec_id or fresh.get("workflowId") != self.workflow_id:
            return unknown("Logs execution identity/workflow contradicts the listing")
        try:
            if envelope_identity(fresh.get("input")) != recorded_envelope:
                return unknown("Logs execution envelope contradicts the listing")
        except (TypeError, ValueError, KeyError, AttributeError):
            return unknown("Malformed logs execution envelope or operation/chain provenance")
        if (fresh.get("status") != status or fresh.get("status") not in TERMINAL_STATUSES
            or (fresh.get("status") == "system_error"
                and fresh.get("errorCode") in {"P-0001", "P-0005"})):
            return unknown("Logs execution lifecycle is unsettled or contradicts the listing")
        collect_execution_hashes(fresh, "logs.execution.transaction_hashes")
        for lg in logs:
            if not isinstance(lg, dict):
                provenance_errors.append("Malformed node log")
                continue
            for key in ("output", "outputRaw"):
                out = lg.get(key)
                if not isinstance(out, dict) or not out.get("transactionHash"):
                    continue
                if (lg.get("executionId") != exec_id or lg.get("nodeId") != ACTION_NODE_ID
                    or lg.get("nodeType") != "web3/write-contract"
                    or lg.get("status") not in NODE_STATUSES
                    or out.get("chainId") != self.chain_id):
                    provenance_errors.append("Wrong execution/node/chain/status in hash-bearing log")
                    continue
                if lg.get("status") not in {"success", "error", "cancelled"}:
                    provenance_errors.append("Hash-bearing node is still in flight")
                add("workflow_execution_logs." + key, out["transactionHash"],
                    f"node={lg.get('nodeId')} nodeStatus={lg.get('status')} RECEIPT NOT VERIFIED")

        if self.db_probe is not None:
            try:
                rows = await self.db_probe.pending_transactions(execution_id=exec_id)
                ev.pending_transactions = rows
                if rows is None:
                    return unknown("Configured pending-transactions probe unavailable; conflicts unchecked")
                for row in rows:
                    if (row.get("execution_id") != exec_id or row.get("chain_id") != self.chain_id
                        or str(row.get("wallet_address", "")).lower() != self.wallet_address.lower()
                        or not isinstance(row.get("nonce"), int) or row["nonce"] < 0
                        or row.get("status") not in {"pending", "confirmed", "failed"}):
                        provenance_errors.append("Wrong execution/wallet/chain/nonce/status in pending transaction")
                        continue
                    add("pending_transactions.tx_hash", row.get("tx_hash"),
                        f"nonce={row['nonce']} rowStatus={row.get('status')} RECEIPT NOT VERIFIED")
            except Exception as exc:
                ev.errors.append(f"pending transaction probe failed: {exc}")
                return unknown("Pending transaction probe failed; conflicts unchecked")

        distinct = {h for _, h, _ in found}
        if provenance_errors:
            ev.errors.extend(provenance_errors)
            return unknown("Hash provenance is inconsistent: " + "; ".join(provenance_errors))
        if len(distinct) > 1:
            return unknown("Conflicting operation-bound hashes: " + ", ".join(sorted(distinct)))
        if status in IN_FLIGHT_STATUSES:
            note = await self._chain_note(execution, ev)
            return unknown(f"Execution {exec_id} is {status!r}, not terminal; "
                f"recorded hashes={sorted(distinct)}. Do not resend." + note)
        if status == "skipped" and found:
            return unknown("Skipped execution contradicts a recorded broadcast hash")
        if found:
            source, txn_hash, note = found[0]
            # Validate the attributed transaction's envelope, not arbitrary matching
            # calldata candidates. This is an additional check, not the op binding.
            try:
                if self.authorization is None:
                    return unknown("SDK journal is not bound; local authorization unchecked")
                env = self.authorization.verify_record(operation_id, txn_hash, record)
                ev.verification = {"localAuthorization": True}
                if self.execution_profile not in {"direct", "sponsored"}:
                    return unknown("Unknown execution profile; cannot infer direct from missing sponsored")
                tx = await self.reconciler.rpc("eth_getTransactionByHash", [txn_hash])
                if self.execution_profile == "sponsored":
                    if self.verification_gate is None:
                        return unknown("Sponsored effect verifier required")
                    ev.verification = await self.verification_gate.verify(
                        operation_id, txn_hash, record, tx, logs, self.reconciler.rpc,
                        binding={"operationId": operation_id, "executionId": exec_id,
                                 "workflowId": self.workflow_id, "nodeId": ACTION_NODE_ID,
                                 "chainId": self.chain_id, "status": status,
                                 "hashReferences": [list(item) for item in found],
                                 "provenance": ev.provenance})
                    if ev.verification.get("ok") is not True:
                        return unknown("SDK authorization/effect gate refused: " +
                                       str(ev.verification.get("blockers")))
                    return self._answer(ev, LookupResult.landed(txn_hash,
                        f"execution {exec_id} terminal; consistent observations of KeeperHub records; "
                        "SDK authorization and receipt effect verified. Sponsored effect only, "
                        "not exclusive calldata or strategy goal; see durable journal proof."
                    ), txn_hash=txn_hash)
                # Direct is an explicitly configured self-host profile. Any
                # positive sponsorship evidence contradicts that profile.
                outputs = [lg.get(k) for lg in logs for k in ("output", "outputRaw")]
                if any(isinstance(o, dict) and (o.get("sponsored") is True or o.get("executedCall") is not None) for o in outputs):
                    return unknown("Sponsored evidence contradicts direct profile")
                if (not tx or str(tx.get("hash", "")).lower() != txn_hash
                    or str(tx.get("from", "")).lower() != self.wallet_address.lower()
                    or str(tx.get("to", "")).lower() != env["to"].lower()
                    or str(tx.get("input", "")).lower() != env["data"].lower()
                    or "value" not in tx or int(tx["value"], 16) != env["value"]):
                    return unknown("Attributed transaction missing or envelope disagrees with recorded operation")
                actual_chain = int(await self.reconciler.rpc("eth_chainId", []), 16)
                if actual_chain != self.chain_id:
                    return unknown("RPC chain disagrees with operation chain")
            except Exception as exc:
                ev.errors.append(f"attributed hash validation failed: {exc}")
                return unknown("Cannot validate attributed transaction envelope")
            return self._answer(ev, LookupResult.landed(txn_hash,
                f"execution {exec_id} terminal ({status}); operation-bound hash from {source} "
                f"({note}); observations of KeeperHub records agree. Envelope checked. "
                "Hash adoption only: SDK must verify receipt; pending/replacement remains possible."
            ), txn_hash=txn_hash)
        return await self._reconcile_against_chain(operation_id, execution, logs, ev)

    async def _scan_for_execution(
        self, execution: dict[str, Any], ev: LookupEvidence
    ) -> tuple[list[Any] | None, dict[str, Any] | None, str | None]:
        """Search the chain for the transaction this run's own input describes.

        Uses no executor-local state: the calldata is re-derived from
        ``workflow_executions.input``, which is KeeperHub's own record of what
        the run was asked to send.
        """
        record = execution.get("input") or {}
        data = encode_from_recorded_input(record)
        if data is None:
            return None, None, "its recorded input could not be re-encoded"
        try:
            value = ether_string_to_wei(record.get("ethValue", "0"))
            lo, hi = await self._block_window(execution)
            matches, scan = await self.reconciler.find(
                sender=self.wallet_address,
                to=record.get("contractAddress"),
                data=data,
                value=value,
                from_block=lo,
                to_block=hi,
            )
        except Exception as exc:  # noqa: BLE001
            ev.errors.append(f"chain reconciliation failed: {exc}")
            return None, None, str(exc)
        ev.chain_scan = scan
        ev.chain_matches = [
            {"hash": m.tx_hash, "block": m.block_number, "nonce": m.nonce}
            for m in matches
        ]
        return matches, scan, None

    async def _chain_note(
        self, execution: dict[str, Any], ev: LookupEvidence
    ) -> str:
        matches, scan, err = await self._scan_for_execution(execution, ev)
        if matches is None:
            return f" Chain could not be searched ({err})."
        if not matches:
            return (f" Diagnostic scan found no candidates in blocks {scan['from_block']}-"
                    f"{scan['to_block']}; this does not prove absence or request quiescence.")
        return (f" Diagnostic scan found {len(matches)} same-calldata candidate(s) "
                f"({', '.join(m.tx_hash for m in matches)}). Candidates do not identify this operation.")

    async def _reconcile_against_chain(
        self, operation_id: str, execution: dict[str, Any],
        logs: list[dict[str, Any]], ev: LookupEvidence,
    ) -> tuple[LookupResult, LookupEvidence]:
        note = await self._chain_note(execution, ev)
        return self._answer(ev, LookupResult.indeterminate(
            f"Execution {execution.get('id')} has no operation-bound hash. "
            "Neither matching calldata nor an empty scan identifies its outcome. "
            "Pre-sign error logs alone do not fence delayed requests; do not resend." + note
        ))

    #: Slack, in seconds, either side of a run's own wall-clock window. A
    #: transaction broadcast just before the run errored can still be mined a
    #: little after `completedAt`, so the upper bound is not exact.
    WINDOW_SLACK_S = 120

    async def _block_window(self, execution: dict[str, Any]) -> tuple[int | None, int]:
        """Narrow the chain scan to the run's own wall-clock window.

        This window only bounds diagnostics. Even one candidate cannot identify
        the operation, and no candidate count changes the lookup decision.
        """
        latest = await self.reconciler.latest_block()
        started = _iso_to_epoch(execution.get("startedAt"))
        if started is None:
            return None, latest
        fork = await self.reconciler.fork_block()
        lo_bound = (fork + 1) if fork is not None else max(0, latest - 20000)
        lo = max(lo_bound, await self.reconciler.block_at_or_after(
            started - self.WINDOW_SLACK_S, lo_bound, latest) - 1)
        completed = _iso_to_epoch(execution.get("completedAt"))
        if completed is None:
            return lo, latest
        hi = await self.reconciler.block_at_or_after(
            completed + self.WINDOW_SLACK_S, lo, latest
        )
        return lo, max(lo, min(hi, latest))

    def _answer(
        self,
        ev: LookupEvidence,
        result: LookupResult,
        *,
        txn_hash: str | None = None,
    ) -> tuple[LookupResult, LookupEvidence]:
        ev.outcome = result.outcome.value
        ev.detail = result.detail or ""
        ev.txn_hash = txn_hash or result.txn_hash
        return result, ev


def _iso_to_epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None
