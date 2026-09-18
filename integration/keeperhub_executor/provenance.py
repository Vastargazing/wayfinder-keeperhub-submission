"""KeeperHub observations, record identities and pinned output derivation.

An observation is one API response. A record is an execution/log row represented
in that response. KeeperHub creates both; repetition adds no trust origin.
This JSON-domain port is checked against the pinned TypeScript source, including
its case-sensitive Set entries and UTF-16 string operations (not a mask heuristic).
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from .workflow import ACTION_NODE_ID

POLICY_VERSION = "keeperhub-provenance-v1"
KEEPERHUB_PIN = "57b7be4d0b4149b47600f5cd2c55299f4f120e83"
REDACTION_SOURCE_SHA256 = "721d17bb32d959c140e15a68c0b1e4a3e6f652e6c55b9e55b69c14f90682a61d"
REDACTION_RULE = f"{KEEPERHUB_PIN}:lib/utils/redact.ts:redactSensitiveData"
# Preserve the source Set literally: upstream lowercases the lookup, NOT this Set.
SENSITIVE_KEYS = {
    "apiKey", "api_key", "apikey", "key", "password", "passwd", "pwd", "secret",
    "token", "accessToken", "access_token", "refreshToken", "refresh_token",
    "privateKey", "private_key", "databaseUrl", "database_url", "connectionString",
    "connection_string", "fromEmail", "from_email", "authorization", "auth", "bearer",
    "creditCard", "credit_card", "cardNumber", "card_number", "cvv", "ssn",
    "phoneNumber", "phone_number", "socialSecurity", "social_security",
}
SENSITIVE_PATTERNS = tuple(re.compile(p, re.I | re.ASCII) for p in (
    r"api[_-]?key", r"secret", r"password", r"credential", r"^auth",
    r"access[_-]?token", r"refresh[_-]?token", r"bearer[_-]?token",
    r"api[_-]?token", r"session[_-]?token",
))


def sensitive_key(key: str) -> bool:
    return key.lower() in SENSITIVE_KEYS or any(p.search(key) for p in SENSITIVE_PATTERNS)


def mask_value(value: str) -> str:
    encoded = value.encode("utf-16-le", errors="surrogatepass")
    length = len(encoded) // 2
    if length == 0:
        return "[REDACTED]"
    if length <= 4:
        return "****"
    return "*" * min(8, length - 4) + encoded[-8:].decode("utf-16-le", errors="surrogatepass")


def redact_sensitive_data(value: Any, depth: int = 0) -> Any:
    """Exact JSON-input behavior of redactObject at KEEPERHUB_PIN."""
    if depth > 10:
        return deepcopy(value)
    if isinstance(value, list):
        return [redact_sensitive_data(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            key: (mask_value(item) if isinstance(item, str) else "[REDACTED]")
            if sensitive_key(key) else redact_sensitive_data(item, depth + 1)
            for key, item in value.items()
        }
    return value


def logs_output(raw: Any) -> Any:
    # step-handler writes redact(raw); the logs route redacts that stored output.
    return redact_sensitive_data(redact_sensitive_data(raw))


def _equal(left: Any, right: Any) -> bool:
    # Do not treat Python True == 1 as a byte-preserving JSON transformation.
    return json.dumps(left, sort_keys=True, ensure_ascii=True) == json.dumps(right, sort_keys=True, ensure_ascii=True)


def _pointer(path: tuple) -> str:
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in path)


def trace_witnesses(log: dict) -> tuple[list[dict], list[dict]]:
    """Validate every present representation; full values come only from the log.

    No authorization/expectation is accepted by this function. Missing trace is
    optional; present null/non-object trace or output is malformed, never absence.
    A paired output is checked at every path before the raw witness is used.
    """
    values = {}
    for name in ("output", "outputRaw"):
        value = log.get(name)
        if value is None:  # nullable output columns represent no output
            continue
        if not isinstance(value, dict):
            raise ValueError(f"malformed {name}")
        if "executedCall" in value and not isinstance(value["executedCall"], dict):
            raise ValueError(f"malformed {name}.executedCall")
        values[name] = value
    out, raw = values.get("output"), values.get("outputRaw")
    fields = []
    if out is not None and raw is not None:
        derived = logs_output(raw)

        def compare(full, transformed, observed, path):
            if isinstance(transformed, dict) and isinstance(observed, dict):
                if transformed.keys() != observed.keys():
                    raise ValueError(f"output derivation field mismatch at {_pointer(path)}")
                for key in transformed:
                    compare(full[key], transformed[key], observed[key], (*path, key))
            elif isinstance(transformed, list) and isinstance(observed, list):
                if len(transformed) != len(observed):
                    raise ValueError(f"output derivation length mismatch at {_pointer(path)}")
                for i, item in enumerate(transformed):
                    compare(full[i], item, observed[i], (*path, i))
            else:
                if not _equal(observed, transformed):
                    raise ValueError(f"output is not the pinned derivation at {_pointer(path)}")
                if not _equal(full, transformed):
                    fields.append({
                        "observation": "execution_logs", "record": log.get("id") or {"executionId": log.get("executionId"), "nodeId": log.get("nodeId")},
                        "source": "KeeperHub", "path": _pointer(("output", *path)),
                        "state": "redacted", "rule": REDACTION_RULE,
                        "applications": ["step-handler.logStepComplete", "logs.GET"],
                        "fullValueFrom": {"observation": "execution_logs", "record": log.get("id") or {"executionId": log.get("executionId"), "nodeId": log.get("nodeId")},
                                          "path": _pointer(("outputRaw", *path))},
                        "derivationVerified": True,
                    })
        compare(raw, derived, out, ())
        # The derivative was checked in full, not ignored or voted away.
        witness = raw
    else:
        witness = raw if raw is not None else out
    if witness is None or "executedCall" not in witness:
        return [], fields
    return [deepcopy(witness["executedCall"])], fields


def record_provenance(execution: dict, fresh: dict, logs: list) -> dict:
    """Group references to records without dropping any API observation.

    Identity conflicts remain in the ledger AND are refused by the executor.
    Missing row IDs get response-local references, not invented shared identities.
    """
    records = {}

    def add(table, row, observation, path):
        identity = row.get("id")
        key = (table, identity) if isinstance(identity, str) and identity else (table, observation, path)
        group = records.setdefault(key, {"table": table, "id": identity,
                                         "origin": "KeeperHub", "operationHashRecord": table == "workflowExecutions" or row.get("nodeId") == ACTION_NODE_ID,
                                         "representations": []})
        group["representations"].append({"observation": observation, "path": path})

    add("workflowExecutions", execution, "executions_listing", "/matchedExecution")
    add("workflowExecutions", fresh, "execution_logs", "/execution")
    for i, log in enumerate(logs):
        if isinstance(log, dict):
            add("workflowExecutionLogs", log, "execution_logs", f"/logs/{i}")
    return {
        "policyVersion": POLICY_VERSION,
        "observations": [
            {"id": "executions_listing", "route": "GET /api/workflows/{workflowId}/executions", "origin": "KeeperHub"},
            {"id": "execution_logs", "route": "GET /api/workflows/executions/{executionId}/logs", "origin": "KeeperHub"},
        ],
        "records": list(records.values()),
        "derivationRule": {"id": REDACTION_RULE, "sourceSha256": REDACTION_SOURCE_SHA256},
        "origins": [{"id": "KeeperHub", "creates": "execution and node-log records",
                     "trustDependsOn": "KeeperHub execution, persistence and API; trace also depends on its RPC"}],
        "sufficiency": "mandatory binding, authorization, lifecycle and conflict checks; no count threshold",
    }


ROOT = Path(__file__).resolve().parents[1]


def source_manifest():
    expected = json.loads((ROOT.parent / 'patches/reconstruction.json').read_text())
    upstream = {}
    for name, pin in expected.items():
        path = ROOT.parent / name / 'upstream'
        def git(*args):
            return subprocess.check_output(['git', '-C', str(path), '-c', 'core.fsmonitor=false', *args], text=True)
        head = git('rev-parse', 'HEAD').strip()
        status = git('status', '--porcelain=v1')
        paths = sorted(line[3:] for line in status.splitlines())
        if head != pin['base'] or paths != sorted(pin['files']):
            raise RuntimeError(f'{name} does not match the pinned patch reconstruction; inspect local work')
        for rel, digest in pin['files'].items():
            if hashlib.sha256((path / rel).read_bytes()).hexdigest() != digest:
                raise RuntimeError(f'{name}/{rel} differs from the supplied reconstruction')
        diff = git('diff', 'HEAD', '--')
        upstream[name] = {'head': head, 'status': status, 'diff': diff,
                          'diff_sha256': hashlib.sha256(diff.encode()).hexdigest(),
                          'changed_source_sha256': pin['files']}
    files = sorted([*ROOT.glob('keeperhub_executor/*.py'), *ROOT.glob('scripts/*.py'),
                    *ROOT.glob('tests/*.py'), ROOT/'pyproject.toml'])
    return {'upstream': upstream, 'integration_files': {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
