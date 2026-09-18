#!/usr/bin/env python3
"""Phase 2.4: re-measure the executions listing on HOSTED KeeperHub.

We previously measured a hard `limit: 50` with no pagination parameters on our
pinned self-host (`app/api/workflows/[workflowId]/executions/route.ts:63-70`).
A finding from one deployment is not carried across to another, so this probes
hosted directly.

Read-only: every request is a GET. The key is read from the environment and is
never written to any output.
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


def scrub(t: str) -> str:
    return t.replace(KEY, "<redacted>") if t else t


def get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=60, trust_env=False) as c:
            r = c.get(f"{BASE}{path}", headers=H, params=params)
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = scrub(r.text[:600])
        return {"status": r.status_code, "body": body}
    except Exception as exc:  # noqa: BLE001
        return {"error": scrub(f"{type(exc).__name__}: {exc}")}


def shape(body: Any) -> dict[str, Any]:
    """Describe the response envelope without dumping its contents."""
    if isinstance(body, list):
        return {"envelope": "bare array", "count": len(body), "keys": None}
    if isinstance(body, dict):
        return {
            "envelope": "object",
            "keys": sorted(body.keys()),
            "count": len(body.get("items", body.get("executions", body.get("runs", []))))
            if any(k in body for k in ("items", "executions", "runs"))
            else None,
            "meta": body.get("meta"),
            "_links": body.get("_links"),
            "nextCursor": body.get("nextCursor"),
        }
    return {"envelope": type(body).__name__}


def main() -> int:
    out: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": BASE,
        "what": "Does hosted still cap /api/workflows/<id>/executions at 50 with no pagination?",
    }

    wfs = get("/api/workflows")
    ids = [w["id"] for w in wfs["body"]] if isinstance(wfs.get("body"), list) else []
    out["workflow_ids"] = ids

    # (a) The per-workflow executions listing, with and without pagination params.
    per_wf: dict[str, Any] = {}
    for wid in ids:
        cases: dict[str, Any] = {}
        base = get(f"/api/workflows/{wid}/executions")
        cases["no params"] = {"status": base["status"], "shape": shape(base["body"])}
        for label, params in [
            ("limit=1", {"limit": 1}),
            ("limit=100", {"limit": 100}),
            ("limit=1000", {"limit": 1000}),
            ("offset=10", {"offset": 10}),
            ("page=2", {"page": 2}),
            ("cursor=x", {"cursor": "x"}),
            ("limit=1&offset=1", {"limit": 1, "offset": 1}),
        ]:
            r = get(f"/api/workflows/{wid}/executions", params)
            cases[label] = {"status": r.get("status"), "shape": shape(r.get("body"))}
        per_wf[wid] = cases
    out["per_workflow_executions"] = per_wf

    # (b) A control: /api/workflows itself DOES validate limit/offset on our
    # pinned source (400 rather than clamp, MAX_PAGE_SIZE=200). If hosted
    # behaves the same, hosted is running code that validates where it means to
    # -- which makes "the executions route silently ignores these" a positive
    # observation rather than a network artefact.
    ctrl: dict[str, Any] = {}
    for label, params in [
        ("limit=1", {"limit": 1}),
        ("limit=200", {"limit": 200}),
        ("limit=201", {"limit": 201}),
        ("limit=0", {"limit": 0}),
        ("offset=1 (no limit)", {"offset": 1}),
        ("limit=1&offset=1", {"limit": 1, "offset": 1}),
    ]:
        r = get("/api/workflows", params)
        body = r.get("body")
        ctrl[label] = {
            "status": r.get("status"),
            "shape": shape(body),
            "error": body.get("error") if isinstance(body, dict) else None,
        }
    out["control_workflows_listing"] = ctrl

    # (c) The org-wide cross-source listing, which our pinned source says DOES
    # paginate (cap 100, cursor = startedAt).
    runs: dict[str, Any] = {}
    for label, params in [
        ("limit=5", {"limit": 5}),
        ("limit=100", {"limit": 100}),
        ("limit=1000", {"limit": 1000}),
        ("page=2", {"page": 2}),
    ]:
        r = get("/api/analytics/runs", params)
        runs[label] = {"status": r.get("status"), "shape": shape(r.get("body"))}
    out["analytics_runs"] = runs

    json.dump(out, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
