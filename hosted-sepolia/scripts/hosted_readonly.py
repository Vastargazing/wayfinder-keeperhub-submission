#!/usr/bin/env python3
"""Phase 2: read-only confirmation against HOSTED KeeperHub.

The API key is read from the environment only. It is never written to any
output file: `scrub()` removes it from every string that leaves this process,
and no request header is ever echoed. Run with:

    set -a; . "$KEEPERHUB_ENV_FILE"; set +a   # e.g. ~/.config/keeperhub/hosted.env, mode 600
    python scripts/hosted_readonly.py > evidence/12-hosted-readonly.json

Nothing here mutates anything: every request is a GET, or an MCP `tools/list` /
read-only `tools/call`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

KEY = os.environ.get("KEEPERHUB_API_KEY", "")
BASE = os.environ.get("KEEPERHUB_BASE_URL", "https://app.keeperhub.com").rstrip("/")
CHAIN = int(os.environ.get("KEEPERHUB_CHAIN_ID", "84532"))
WALLET = os.environ.get("KEEPERHUB_WALLET_ADDRESS", "")

if not KEY:
    print(json.dumps({"error": "KEEPERHUB_API_KEY is not set"}))
    sys.exit(2)


def scrub(text: str) -> str:
    """Remove the key from anything that leaves this process."""
    if not text:
        return text
    out = text.replace(KEY, "<KEEPERHUB_API_KEY:redacted>")
    # also catch a key that appears without its prefix, or truncated in an echo
    if len(KEY) > 12:
        out = out.replace(KEY[3:], "<redacted>")
    return out


def scrub_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [scrub_obj(o) for o in obj]
    if isinstance(obj, dict):
        return {k: scrub_obj(v) for k, v in obj.items()}
    return obj


HEADERS = {"Authorization": f"Bearer {KEY}", "accept": "application/json"}


def get(path: str, **params: Any) -> dict[str, Any]:
    url = f"{BASE}{path}"
    started = time.time()
    try:
        with httpx.Client(timeout=60, trust_env=False) as c:
            r = c.get(url, headers=HEADERS, params=params or None)
        body: Any
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = r.text[:2000]
        return {
            "path": path,
            "params": params,
            "status": r.status_code,
            "ms": int((time.time() - started) * 1000),
            "body": scrub_obj(body),
        }
    except Exception as exc:  # noqa: BLE001
        return {"path": path, "params": params, "error": scrub(f"{type(exc).__name__}: {exc}")}


def mcp(method: str, params: dict[str, Any] | None = None, req_id: int = 1) -> dict[str, Any]:
    """One MCP JSON-RPC call over the org-scoped transport."""
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    try:
        with httpx.Client(timeout=90, trust_env=False) as c:
            r = c.post(
                f"{BASE}/mcp",
                headers={
                    "Authorization": f"Bearer {KEY}",
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
                json=payload,
            )
        text = r.text
        body: Any
        if text.startswith("event:") or "\ndata:" in text:
            # SSE framing: take the last data: line
            data_lines = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
            try:
                body = json.loads(data_lines[-1]) if data_lines else text[:2000]
            except Exception:  # noqa: BLE001
                body = text[:2000]
        else:
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = text[:2000]
        return {"method": method, "status": r.status_code, "body": scrub_obj(body)}
    except Exception as exc:  # noqa: BLE001
        return {"method": method, "error": scrub(f"{type(exc).__name__}: {exc}")}


def main() -> int:
    out: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": BASE,
        "chain_id": CHAIN,
        "wallet_from_env": WALLET,
        "note": "API key read from the environment; never written here.",
    }

    # --- 1. identity: whose key is this? ----------------------------------
    out["keys"] = get("/api/keys")
    out["wallet"] = get("/api/user/wallet")
    out["organization"] = get("/api/organization")

    # --- 2. chains, as this org sees them ---------------------------------
    out["chains"] = get("/api/chains")

    # --- 3. workflows ------------------------------------------------------
    out["workflows_authed"] = get("/api/workflows")
    out["workflows_paged"] = get("/api/workflows", limit=200)

    # --- 4. gas sponsorship, org-scoped ------------------------------------
    out["billing_gas_sponsorship"] = get("/api/billing/gas-sponsorship", includeTestnets="true")

    # --- 5. analytics runs (cross-source execution listing) ----------------
    out["analytics_runs"] = get("/api/analytics/runs", limit=5)

    # --- 6. MCP surface ----------------------------------------------------
    out["mcp_initialize"] = mcp(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "hosted-sepolia-readonly", "version": "0.1.0"},
        },
    )
    out["mcp_tools_list"] = mcp("tools/list", {}, req_id=2)

    json.dump(scrub_obj(out), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
