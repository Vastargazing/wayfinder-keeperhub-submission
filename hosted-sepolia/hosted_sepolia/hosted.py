"""Thin read-only client for the hosted KeeperHub endpoints this work needs.

STATUS: the pure verification core (`verify.py`, `events.py`) is unit-tested.
**This I/O layer has not been exercised against a real KeeperHub execution**,
because producing one requires a Base Sepolia broadcast, which is gated on human
confirmation. Treat every shape assumption here as UNVERIFIED-at-runtime; they
are read from the pinned source, and the field names are asserted rather than
guessed so a shape change fails loudly instead of silently returning None.

Which endpoints carry a hash, OBSERVED on hosted 2026-09-06
(evidence/14-hosted-hash-sources.json). Two distinct 404 shapes separate
"the route exists, this id does not" from "no such route":

  reachable, routed:
    GET /api/workflows/<wf>/executions              -> rows incl. transactionHashes
    GET /api/workflows/executions/<id>/status       -> lifecycle
    GET /api/workflows/executions/<id>/logs         -> node output + executedCall
    GET /api/execute/<id>/status                    -> top-level `sponsored`
    GET /api/analytics/runs                         -> org-wide, paginated
    GET /api/billing/gas-sponsorship                -> monthly AGGREGATES only

  no route at all:
    /api/pending-transactions        -> "Route GET ... not found"
    /api/idempotency-records         -> "Route GET ... not found"

  routed but not usable with an API key:
    GET /api/analytics/runs/<id>/steps -> 403 "Organization not found" (session auth)

The consequence is in REPORT.md §5: on hosted, `pending_transactions` is
unavailable, and on the sponsored path it is never written in the first place.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from hosted_sepolia.verify import ModePayload, classify_execution_mode

ACTION_NODE_TYPE = "web3/write-contract"


class HostedKeeperHub:
    """Read-only. Every method here is a GET.

    The API key is taken from the environment and is never logged, never
    returned, and never stored on the instance in a form that a `repr` would
    print.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._key = api_key or os.environ["KEEPERHUB_API_KEY"]
        self.base_url = (base_url or os.environ.get("KEEPERHUB_BASE_URL", "https://app.keeperhub.com")).rstrip("/")
        self._timeout = timeout
        self._transport = transport

    def __repr__(self) -> str:  # never leak the key through a traceback
        return f"HostedKeeperHub(base_url={self.base_url!r}, api_key=<redacted>)"

    def scrub(self, text: str) -> str:
        return text.replace(self._key, "<redacted>") if text else text

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        with httpx.Client(timeout=self._timeout, trust_env=False, transport=self._transport) as c:
            r = c.get(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self._key}", "accept": "application/json"},
                params=params,
            )
        if r.status_code >= 400:
            raise HostedError(r.status_code, self.scrub(r.text[:600]), path)
        return r.json()

    # --- ownership: find the run this operation id started -----------------

    def executions(self, workflow_id: str) -> list[dict[str, Any]]:
        """The per-workflow listing.

        Saved evidence/13 contains empty bare arrays. The supplied pagination
        parameters were accepted, but empty results cannot establish whether
        they are used. A cap of 50 without pagination is SOURCE at the local pin;
        hosted page-boundary behavior remains unverified.
        """
        rows = self._get(f"/api/workflows/{workflow_id}/executions")
        if not isinstance(rows, list):
            raise HostedError(200, f"expected a bare array, got {type(rows).__name__}", "executions")
        return rows

    def find_by_operation_id(self, workflow_id: str, operation_id: str) -> list[dict[str, Any]]:
        """Every run whose recorded input carries this operation id.

        More than one is a conflict the caller must refuse, not choose between.
        """
        out = []
        for row in self.executions(workflow_id):
            record = row.get("input")
            if isinstance(record, dict) and record.get("operationId") == operation_id:
                out.append(row)
        return out

    def execution_logs(self, execution_id: str) -> list[dict[str, Any]]:
        payload = self._get(f"/api/workflows/executions/{execution_id}/logs")
        logs = payload.get("logs") if isinstance(payload, dict) else None
        if not isinstance(logs, list):
            raise HostedError(200, "logs payload has no `logs` array", "execution_logs")
        return logs

    def execution_status(self, execution_id: str) -> dict[str, Any]:
        return self._get(f"/api/workflows/executions/{execution_id}/status")

    def direct_execution_status(self, execution_id: str) -> dict[str, Any]:
        """Carries a top-level `sponsored` boolean.

        Caveat from the pinned source: it is `Boolean(output?.sponsored)`
        (`app/api/execute/[executionId]/status/route.ts:87`) and the direct
        branch never writes `sponsored: false`, so `false` here means "direct
        **or** never written". `classify_execution_mode` treats that as UNKNOWN.
        """
        return ModePayload(self._get(f"/api/execute/{execution_id}/status"), "status_boolean")

    def execution_mode(self, execution_id: str, node_id: str | None = None):
        """Compatibility entry point for workflow executions only."""
        return self.workflow_execution_mode(execution_id, node_id)

    def workflow_execution_mode(self, execution_id: str, node_id: str | None = None):
        """Classify one workflow node from its persisted output and outputRaw."""
        out, raw, _ = self.write_contract_node_output(execution_id, node_id)
        return classify_execution_mode(out, raw)

    def direct_execution_mode(self, execution_id: str):
        """Classify directExecutions status/result, a separate ID namespace."""
        return classify_execution_mode(self.direct_execution_status(execution_id))

    def write_contract_node_output(
        self, execution_id: str, node_id: str | None = None
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        """Return `(output, outputRaw, log)` for the write-contract node.

        Refuses to guess when a run has more than one such node: an executor
        workflow has exactly one, and picking one of several would be an
        attribution decision this function has no basis to make.
        """
        matching = [
            lg
            for lg in self.execution_logs(execution_id)
            if isinstance(lg, dict)
            and lg.get("nodeType") == ACTION_NODE_TYPE
            and (node_id is None or lg.get("nodeId") == node_id)
        ]
        if len(matching) != 1:
            raise HostedError(
                200,
                f"expected exactly one {ACTION_NODE_TYPE} node log, found {len(matching)}",
                "write_contract_node_output",
            )
        log = matching[0]
        out = log.get("output") if isinstance(log.get("output"), dict) else None
        raw = log.get("outputRaw") if isinstance(log.get("outputRaw"), dict) else None
        return out, raw, log


class HostedError(RuntimeError):
    def __init__(self, status: int, body: str, path: str) -> None:
        super().__init__(f"{path}: HTTP {status}: {body}")
        self.status = status
        self.body = body
        self.path = path
