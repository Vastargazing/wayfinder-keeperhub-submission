"""F1–F3 acceptance regressions: real clients/SDK, offline HTTP/RPC models."""

import json
from copy import deepcopy

import httpx
import pytest
from hosted_sepolia.hosted import HostedKeeperHub
from wayfinder_paths.core.utils.executor import ExecutionOutcomeUnknownError

from tests.fixtures import (
    AMOUNT,
    AUSDC,
    CHAIN_ID,
    POOL,
    STRANGER,
    USDC,
    WALLET,
    sponsored_supply_receipt,
    supply_log,
    transfer_log,
)
from tests.test_recovery import World, close, envelope
from tests.test_rework_regressions import check


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "hash",
        "execution_id",
        "workflow_id",
        "operation_id",
        "chain",
        "target",
        "value",
        "calldata",
        "running",
        "different_terminal",
        "reclaimable",
        "missing_execution",
        "malformed_input",
        "malformed_hashes",
        "hash_node",
        "hash_chain",
    ],
)
async def test_fresh_execution_contradiction_never_advances_journal(tmp_path, fault):
    w = World()
    ex = await w.open(tmp_path)
    try:
        w.lose_response = True
        with pytest.raises(ExecutionOutcomeUnknownError):
            await ex.send(envelope())
        original = deepcopy(ex.journal.entries())
        op = original[0]["operation_id"]
        fresh = deepcopy(w.rows[0])
        w.logs["exec-1"]["execution"] = fresh
        if fault == "hash":
            fresh["transactionHashes"][0]["hash"] = "0x" + "ab" * 32
        elif fault == "execution_id":
            fresh["id"] = "other-execution"
        elif fault == "workflow_id":
            fresh["workflowId"] = "other-workflow"
        elif fault == "operation_id":
            fresh["input"]["operationId"] = "other-operation"
        elif fault == "chain":
            fresh["input"]["network"] = "8453"
        elif fault == "target":
            fresh["input"]["contractAddress"] = STRANGER
        elif fault == "value":
            fresh["input"]["ethValue"] = "1"
        elif fault == "calldata":
            fresh["input"]["functionArgs"][1] = str(AMOUNT + 1)
        elif fault == "running":
            fresh["status"] = "running"
        elif fault == "different_terminal":
            fresh["status"] = "error"
        elif fault == "reclaimable":
            fresh.update(status="system_error", errorCode="P-0001")
        elif fault == "missing_execution":
            del w.logs["exec-1"]["execution"]
        elif fault == "malformed_input":
            fresh["input"] = ["not an envelope"]
        elif fault == "malformed_hashes":
            fresh["transactionHashes"] = "not a list"
        elif fault == "hash_node":
            fresh["transactionHashes"][0]["nodeId"] = "other-node"
        elif fault == "hash_chain":
            fresh["transactionHashes"][0]["chainId"] = 8453
        result = await ex.executor.lookup(op)
        assert result.outcome.value == "indeterminate"
        for _ in range(2):
            with pytest.raises(ExecutionOutcomeUnknownError):
                await ex.resolve_pending(CHAIN_ID)
            with pytest.raises(ExecutionOutcomeUnknownError):
                await ex.send(envelope())
            assert ex.journal.entries() == original
        assert w.posts == [op]
        assert (
            ex.journal._conn.execute(
                "SELECT COUNT(*) FROM hosted_verification"
            ).fetchone()[0]
            == 0
        )
    finally:
        await close(ex)


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_only_hash", [False, True])
async def test_agreeing_execution_snapshots_recover(tmp_path, fresh_only_hash):
    w = World()
    ex = await w.open(tmp_path)
    try:
        w.lose_response = True
        with pytest.raises(ExecutionOutcomeUnknownError):
            await ex.send(envelope())
        op = ex.journal.entries()[0]["operation_id"]
        w.logs["exec-1"]["execution"] = deepcopy(w.rows[0])
        expected_hash = w.rows[0]["transactionHashes"][0]["hash"]
        if fresh_only_hash:
            w.rows[0]["transactionHashes"] = []
            for key in ("output", "outputRaw"):
                del w.logs["exec-1"]["logs"][0][key]["transactionHash"]
        assert (await ex.executor.lookup(op)).outcome.value == "landed"
        await ex.resolve_pending(CHAIN_ID)
        row = ex.journal.entries()[0]
        assert row["state"] == "submitted" and row["txn_hash"] == expected_hash
        assert w.posts == [op]
        proof = json.loads(
            ex.journal._conn.execute(
                "SELECT proof FROM hosted_verification WHERE operation_id=?", (op,)
            ).fetchone()[0]
        )
        assert "logs.execution.transaction_hashes" in {
            source[0] for source in proof["ownership"]["hashReferences"]
        }
    finally:
        await close(ex)


def test_workflow_mode_does_not_request_direct_table():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/api/workflows/executions/workflow-only/logs":
            return httpx.Response(
                200,
                json={
                    "logs": [
                        {
                            "nodeType": "web3/write-contract",
                            "nodeId": "action",
                            "output": {"sponsored": True},
                            "outputRaw": {"sponsored": True},
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": "Execution not found"})

    client = HostedKeeperHub(
        base_url="https://offline.invalid",
        api_key="fixture",
        transport=httpx.MockTransport(handler),
    )
    assert client.execution_mode("workflow-only").mode.value == "sponsored"
    assert paths == ["/api/workflows/executions/workflow-only/logs"]


def nft_transfer(emitter):
    log = transfer_log(emitter=emitter, sender=WALLET, to=POOL, value=1)
    log["topics"].append(log["data"])
    log["data"] = "0x"
    return log


def test_unrelated_erc721_transfer_does_not_reject_supply():
    receipt = sponsored_supply_receipt()
    receipt["logs"].append(nft_transfer(STRANGER))
    assert check(receipt=receipt).ok


@pytest.mark.parametrize("emitter", [USDC, AUSDC, POOL])
def test_incompatible_abi_at_relevant_emitter_is_still_rejected(emitter):
    receipt = sponsored_supply_receipt()
    receipt["logs"].append(nft_transfer(emitter))
    assert not check(receipt=receipt).ok


def test_malformed_relevant_supply_cannot_hide_behind_unrelated_log():
    receipt = sponsored_supply_receipt()
    malformed = supply_log()
    malformed["data"] = "0x"
    receipt["logs"].extend([nft_transfer(STRANGER), malformed])
    assert not check(receipt=receipt).ok


def test_foreign_emitter_cannot_supply_required_effect():
    receipt = sponsored_supply_receipt(logs=[supply_log(emitter=STRANGER)])
    assert not check(receipt=receipt).ok
