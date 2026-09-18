"""The one parameterised KeeperHub workflow this executor drives.

**Decision: one workflow, created once, executed per operation with inputs.**
Not one workflow per operation. Reasons, in the order they mattered:

1. **Lookup needs an index.** ``GET /api/workflows/<id>/executions`` is the only
   API that lists runs, and it is scoped to a single workflow. With one
   workflow, every operation this executor ever started is a row in one list and
   ``lookup`` is one request. With a workflow per operation, answering "did
   operation X run?" means enumerating workflows — and if the create call is the
   thing that was lost in a crash, there is no id to enumerate *by*, so the
   answer becomes unprovable exactly when it matters.
2. **The idempotency key is scoped per workflow.** ``beginIdempotentFromRequest``
   is called with ``scope: `workflow-execute:${workflowId}``` (SOURCE,
   app/api/workflow/[workflowId]/execute/route.ts:212-217) and the unique index
   is ``(organization_id, scope, idempotency_key)``. One workflow ⇒ one
   namespace ⇒ the operation id is globally unique for this executor. A
   per-operation workflow would put every key in its own namespace, which makes
   the idempotency guarantee vacuous.
3. **One intended definition.** This integration assumes the fixed, single-action
   parameterized workflow it created. KeeperHub may mutate workflow definitions;
   equal executedWorkflowHash/ranVersion is not an enforced immutable guarantee.
4. Creating a workflow costs a round trip and a row per transaction.

The cost of the choice: every field of the write-contract node is a template,
so a malformed input payload fails at template resolution rather than at
workflow-save time. That is acceptable because resolution is **fail-closed** —
``assertResolved`` always throws on an unresolved reference (SOURCE,
lib/workflow/executor/template-resolution.ts:99, KEEP-525 removed the legacy
silent-substitute mode), so a missing field aborts the node instead of
broadcasting ``""``.

Template form. ``processTemplates`` recognises exactly two patterns (SOURCE,
lib/workflow/executor/executor.workflow.ts:948-951): stored
``{{@nodeId:Label.path}}`` and display ``{{Label.path}}``. We use the stored
form, which resolves by *node id* (``outputs[sanitizedNodeId]``,
executor.workflow.ts:852) and is therefore immune to a label rename. There is no
``{{trigger.input.x}}`` namespace; the Manual trigger spreads the ``input``
object flat onto its own output data (executor.workflow.ts:3095-3103,
``deserializeTriggerInput`` is a pass-through for Manual), so the path is
``data.<key>``.

``functionArgs`` and ``abi`` are sent as **arrays** in the input payload, not as
pre-stringified JSON: ``formatConfigValue`` JSON-stringifies an array
(executor.workflow.ts:772-789) and ``write-contract-core`` then ``JSON.parse``s
the field, so the array survives one serialise/parse round trip with no
hand-escaping. Per-element templating (``"[\\"{{...}}\\"]"``) is the form the
docs suggest and it is strictly worse here: ``formatConfigValue`` inlines
strings raw, so any value containing a quote would break the surrounding JSON.
"""

from __future__ import annotations

from typing import Any

TRIGGER_NODE_ID = "trigger-1"
ACTION_NODE_ID = "action-1"
TRIGGER_LABEL = "Manual"  # must not contain a '.': replaceConfigTemplate splits on the first one
ACTION_LABEL = "Submit Wayfinder Envelope"


def _ref(field: str) -> str:
    return f"{{{{@{TRIGGER_NODE_ID}:{TRIGGER_LABEL}.data.{field}}}}}"


#: The `input` keys the workflow expects. `operationId` is not consumed by the
#: write-contract node at all — it is carried purely so that KeeperHub's own
#: `workflow_executions.input` column records which operation a run belongs to.
#: That column is written before dispatch. Its absence does not fence a delayed
#: request and cannot prove `never_seen` (see KeeperHubExecutor.lookup).
INPUT_KEYS = (
    "operationId",
    "network",
    "contractAddress",
    "abi",
    "abiFunction",
    "functionArgs",
    "ethValue",
)


def build_workflow_definition(name: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "nodes": [
            {
                "id": TRIGGER_NODE_ID,
                "type": "trigger",
                "position": {"x": 0, "y": 0},
                "data": {
                    "label": TRIGGER_LABEL,
                    "type": "trigger",
                    "status": "idle",
                    "config": {"triggerType": "Manual"},
                },
            },
            {
                "id": ACTION_NODE_ID,
                "type": "action",
                "position": {"x": 320, "y": 0},
                "data": {
                    "label": ACTION_LABEL,
                    "type": "action",
                    "status": "idle",
                    "config": {
                        "actionType": "web3/write-contract",
                        "network": _ref("network"),
                        "contractAddress": _ref("contractAddress"),
                        "abi": _ref("abi"),
                        "abiFunction": _ref("abiFunction"),
                        "functionArgs": _ref("functionArgs"),
                        "ethValue": _ref("ethValue"),
                    },
                },
            },
        ],
        "edges": [
            {"id": "e1", "source": TRIGGER_NODE_ID, "target": ACTION_NODE_ID},
        ],
        "inputSchema": {
            # Declarative only. NOTHING validates the execute payload against
            # this (OBSERVED + SOURCE: the route casts `body.input` unchecked,
            # app/api/workflow/[workflowId]/execute/route.ts:199-208). It is
            # here as documentation and for the OpenAPI/MCP surfaces.
            "type": "object",
            "required": list(INPUT_KEYS),
            "properties": {
                "operationId": {"type": "string"},
                "network": {"type": "string"},
                "contractAddress": {"type": "string"},
                "abi": {"type": "array"},
                "abiFunction": {"type": "string"},
                "functionArgs": {"type": "array"},
                "ethValue": {"type": "string"},
            },
        },
    }
