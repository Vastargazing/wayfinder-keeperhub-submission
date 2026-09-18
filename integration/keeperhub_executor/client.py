"""Thin async client for the KeeperHub HTTP API surface this executor uses.

Only four endpoints are needed, and every one of them is authenticated with an
organization API key (``Authorization: Bearer kh_...``), the path the stand
established (research/stand/REPORT.md, "Session / auth"):

| purpose                | endpoint                                            |
| ---------------------- | --------------------------------------------------- |
| create the workflow    | ``POST /api/workflows/create``                       |
| submit one operation   | ``POST /api/workflow/<id>/execute`` + Idempotency-Key |
| find an operation      | ``GET  /api/workflows/<id>/executions``              |
| read per-node output   | ``GET  /api/workflows/executions/<execId>/logs``     |

``trust_env=False`` is not optional: this machine has ``HTTP_PROXY``/
``HTTPS_PROXY`` set and httpx honours them by default, which sends
``localhost:3000`` through a proxy (SEAM.md §14 hit the same thing on the RPC
side).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

# GET /api/workflows/<id>/executions is `limit: 50` with no pagination
# (app/api/workflows/[workflowId]/executions/route.ts:69). That cap is load
# bearing for `never_seen` — see KeeperHubExecutor.lookup.
EXECUTIONS_PAGE_CAP = 50


class KeeperHubApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        self.status = status
        self.body = body
        super().__init__(message)


@dataclass
class ExecuteOutcome:
    """What ``POST /execute`` said, normalised.

    ``kind`` is one of:

    - ``started``  — a fresh run; ``execution_id`` is set.
    - ``replay``   — this Idempotency-Key already ran; ``execution_id`` is the
      *original* run's id (KeeperHub annotates the body ``idempotentReplay``).
      **No second run was started.**
    - ``in_progress`` — 409 ``idempotency_in_progress``: a request with this key
      still holds the processing lock. No second run; outcome not yet knowable.
    - ``conflict`` — 409 ``idempotency_conflict``: the key is bound to a
      *different* request body. Never rotate the key here; the body drifted.
    """

    kind: str
    status: int
    body: Any
    execution_id: str | None = None
    replayed: bool = False


class KeeperHubClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _json(self, method: str, path: str, **kw: Any) -> tuple[int, Any]:
        resp = await self._client.request(method, f"{self.base_url}{path}", **kw)
        try:
            body: Any = resp.json()
        except Exception:  # noqa: BLE001
            body = resp.text
        return resp.status_code, body

    async def health(self) -> Any:
        _, body = await self._json("GET", "/api/health")
        return body

    async def create_workflow(self, definition: dict[str, Any]) -> dict[str, Any]:
        status, body = await self._json(
            "POST", "/api/workflows/create", json=definition
        )
        if status != 200 or not isinstance(body, dict) or "id" not in body:
            raise KeeperHubApiError(
                f"create_workflow failed: HTTP {status}", status=status, body=body
            )
        return body

    async def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        status, body = await self._json("GET", f"/api/workflows/{workflow_id}")
        if status == 404:
            return None
        if status != 200:
            raise KeeperHubApiError(
                f"get_workflow failed: HTTP {status}", status=status, body=body
            )
        return body if isinstance(body, dict) else None

    async def execute(
        self,
        workflow_id: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ExecuteOutcome:
        """Start (or replay) one run. The idempotency key is the operation id."""
        status, body = await self._json(
            "POST",
            f"/api/workflow/{workflow_id}/execute",
            json={"input": payload},
            headers={"Idempotency-Key": idempotency_key},
        )
        code = body.get("code") if isinstance(body, dict) else None
        if status == 409 and code == "idempotency_in_progress":
            return ExecuteOutcome("in_progress", status, body)
        if status == 409 and code == "idempotency_conflict":
            return ExecuteOutcome(
                "conflict",
                status,
                body,
                execution_id=body.get("originalExecutionId"),
            )
        if status != 200 or not isinstance(body, dict):
            raise KeeperHubApiError(
                f"execute failed: HTTP {status}", status=status, body=body
            )
        replayed = bool(body.get("idempotentReplay"))
        return ExecuteOutcome(
            "replay" if replayed else "started",
            status,
            body,
            execution_id=body.get("executionId"),
            replayed=replayed,
        )

    async def executions(self, workflow_id: str) -> list[dict[str, Any]]:
        status, body = await self._json(
            "GET", f"/api/workflows/{workflow_id}/executions"
        )
        if status != 200 or not isinstance(body, list):
            raise KeeperHubApiError(
                f"executions failed: HTTP {status}", status=status, body=body
            )
        return body

    async def execution_logs(self, execution_id: str) -> dict[str, Any]:
        status, body = await self._json(
            "GET", f"/api/workflows/executions/{execution_id}/logs"
        )
        if status != 200 or not isinstance(body, dict):
            raise KeeperHubApiError(
                f"execution_logs failed: HTTP {status}", status=status, body=body
            )
        return body
