#!/usr/bin/env python3
"""Phase 2.5: which hash sources are reachable over HTTP on hosted?

Probes each candidate endpoint with a syntactically valid but non-existent id,
to establish that the route exists and is reachable with this key, without
needing a real execution. A 404/"not found" from a routed endpoint is a
different observation from a 404 "Route ... not found".

Read-only. The key is read from the environment and never written out.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

KEY = os.environ["KEEPERHUB_API_KEY"]
BASE = os.environ.get("KEEPERHUB_BASE_URL", "https://app.keeperhub.com").rstrip("/")
H = {"Authorization": f"Bearer {KEY}", "accept": "application/json"}
FAKE_EXEC = "zzzzzzzzzzzzzzzzzzzzz"  # 21 chars, the nanoid shape KeeperHub uses


def scrub(t: str) -> str:
    return t.replace(KEY, "<redacted>") if t else t


def get(path: str, params: dict | None = None) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=60, trust_env=False) as c:
            r = c.get(f"{BASE}{path}", headers=H, params=params)
        try:
            body: Any = r.json()
        except Exception:  # noqa: BLE001
            body = scrub(r.text[:400])
        return {"path": path, "status": r.status_code, "body": body}
    except Exception as exc:  # noqa: BLE001
        return {"path": path, "error": scrub(f"{type(exc).__name__}: {exc}")}


def main() -> int:
    wfs = get("/api/workflows")
    wid = wfs["body"][0]["id"] if isinstance(wfs.get("body"), list) and wfs["body"] else "unknown"

    out: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": BASE,
        "probe_execution_id": FAKE_EXEC,
        "real_workflow_id_used_for_routing": wid,
        "candidates": {
            "workflow_executions.transaction_hashes": get(f"/api/workflows/{wid}/executions"),
            "workflow execution status": get(f"/api/workflows/executions/{FAKE_EXEC}/status"),
            "workflow execution logs (node output / executedCall)": get(
                f"/api/workflows/executions/{FAKE_EXEC}/logs"
            ),
            "direct execution status (carries top-level `sponsored`)": get(
                f"/api/execute/{FAKE_EXEC}/status"
            ),
            "analytics runs (org-wide, paginated)": get("/api/analytics/runs", {"limit": 1}),
            "analytics run steps (session-only per pinned source)": get(
                f"/api/analytics/runs/{FAKE_EXEC}/steps"
            ),
            "billing gas sponsorship (aggregate only, no hashes)": get(
                "/api/billing/gas-sponsorship", {"includeTestnets": "true"}
            ),
            "pending_transactions (expected: no route at all)": get("/api/pending-transactions"),
            "idempotency_records (expected: no route at all)": get("/api/idempotency-records"),
        },
    }
    json.dump(out, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
