"""Offline acceptance through the real SDK journal/executor/gate, zero POSTs."""
from copy import deepcopy
import json

import pytest

from keeperhub_executor.provenance import (
    POLICY_VERSION, REDACTION_RULE, logs_output, redact_sensitive_data,
)
from tests.test_recovery import World, abi_resolver, close, envelope
from tests.fixtures import (
    AMOUNT, CHAIN_ID, HASH, USDC, WALLET, STRANGER, WRAPPER, ZERO,
    sponsored_tx, sponsored_supply_receipt, transfer_log,
)
from keeperhub_executor.workflow import ACTION_NODE_ID
from hosted_sepolia.recovery import FAUCET
from wayfinder_paths.core.utils.executor import ExecutionOutcomeUnknownError


class ReadOnlyWorld(World):
    def http(self, request):
        if request.method != "GET":
            self.posts.append(request.method)
            raise AssertionError("offline replay prohibits HTTP writes")
        return super().http(request)


async def prepared(tmp_path):
    world = ReadOnlyWorld()
    execution = await world.open(tmp_path, allow=False)
    env = envelope("erc20_mint")
    op = execution.journal.begin(env)
    _, record = execution.executor.prepare(env)
    record["operationId"] = op
    row = {"id": "exec-1", "workflowId": "wf", "status": "success", "input": record,
           "transactionHashes": [{"nodeId": ACTION_NODE_ID, "chainId": CHAIN_ID, "hash": HASH}]}
    raw = {"success": True, "sponsored": True, "transactionHash": HASH, "chainId": CHAIN_ID,
           "executedCall": {"contractAddress": FAUCET, "functionName": "mint",
                            "args": {"token": USDC, "to": WALLET, "amount": str(AMOUNT)},
                            "reverted": False, "sponsored": True, "topLevelTo": WRAPPER}}
    log = {"id": "log-1", "executionId": "exec-1", "nodeId": ACTION_NODE_ID,
           "nodeType": "web3/write-contract", "status": "success",
           "outputRaw": raw, "output": logs_output(raw)}
    world.rows = [row]
    world.logs = {"exec-1": {"execution": deepcopy(row), "logs": [log]}}
    world.txs = {HASH: sponsored_tx()}
    world.receipts = {HASH: sponsored_supply_receipt(logs=[
        transfer_log(emitter=USDC, sender=ZERO, to=WALLET, value=AMOUNT)])}
    return world, execution, op, log


def proof(ex, op):
    return json.loads(ex.journal._conn.execute(
        "SELECT proof FROM hosted_verification WHERE operation_id=?", (op,)).fetchone()[0])


async def refused(world, ex, op, tmp_path):
    before = ex.journal.entries()
    for _ in range(2):
        result = await ex.executor.lookup(op)
        assert result.outcome.value == "indeterminate"
        with pytest.raises(ExecutionOutcomeUnknownError):
            await ex.resolve_pending(CHAIN_ID)
        assert ex.journal.entries() == before
        assert world.posts == [] and ex.executor.submits == 0
        await close(ex)
        ex = await world.open(tmp_path, allow=False)
    await close(ex)


@pytest.mark.asyncio
async def test_derived_trace_accepts_and_reopens_without_post(tmp_path):
    world, ex, op, log = await prepared(tmp_path)
    assert await ex.send(envelope("erc20_mint")) == HASH
    p = proof(ex, op)
    assert p["ok"] and p["policyVersion"] == POLICY_VERSION
    assert p["redactions"] == [{
        "observation": "execution_logs", "record": "log-1", "source": "KeeperHub",
        "path": "/output/executedCall/args/token", "state": "redacted", "rule": REDACTION_RULE,
        "applications": ["step-handler.logStepComplete", "logs.GET"],
        "fullValueFrom": {"observation": "execution_logs", "record": "log-1",
                          "path": "/outputRaw/executedCall/args/token"}, "derivationVerified": True}]
    original = ex.journal.entries()
    assert len(original) == 1 and original[0]["operation_id"] == op
    assert original[0]["txn_hash"] == HASH and original[0]["consumed"]
    await close(ex)
    for _ in range(2):
        ex = await world.open(tmp_path, allow=False)
        assert (await ex.executor.lookup(op)).outcome.value == "landed"
        await ex._validate_result(op, HASH)
        assert ex.journal.entries() == original
        assert world.posts == [] and ex.executor.submits == 0
        await close(ex)


@pytest.mark.asyncio
async def test_endpoints_are_observations_of_one_record(tmp_path):
    world, ex, op, _ = await prepared(tmp_path)
    assert (await ex.executor.lookup(op)).outcome.value == "landed"
    ledger = proof(ex, op)["provenance"]
    assert len(ledger["observations"]) == 2
    assert len(ledger["origins"]) == 1 and ledger["origins"][0]["id"] == "KeeperHub"
    assert len(ledger["records"]) == 2
    execution = next(r for r in ledger["records"] if r["table"] == "workflowExecutions")
    assert [r["observation"] for r in execution["representations"]] == ["executions_listing", "execution_logs"]
    assert "independent_sources" not in json.dumps(proof(ex, op))
    await close(ex)


@pytest.mark.asyncio
async def test_ten_old_hashes_cannot_overrule_fresh_record(tmp_path):
    world, ex, op, _ = await prepared(tmp_path)
    world.rows[0]["transactionHashes"] *= 10
    world.logs["exec-1"]["execution"]["transactionHashes"][0]["hash"] = "0x" + "bb" * 32
    events = []
    ex.executor.on_evidence = lambda kind, payload: events.append(payload)
    assert (await ex.executor.lookup(op)).outcome.value == "indeterminate"
    assert "Conflicting" in events[-1]["detail"]
    record = events[-1]["provenance"]["records"][0]
    assert len(record["representations"]) == 2
    await refused(world, ex, op, tmp_path)


@pytest.mark.asyncio
async def test_single_hash_observation_is_sufficient_when_checks_pass(tmp_path):
    world, ex, op, log = await prepared(tmp_path)
    world.rows[0]["transactionHashes"] = []
    world.logs["exec-1"]["execution"]["transactionHashes"] = []
    log["output"] = log.pop("outputRaw")  # standalone full trace
    assert (await ex.executor.lookup(op)).outcome.value == "landed"
    assert len(proof(ex, op)["ownership"]["hashReferences"]) == 1
    await ex.resolve_pending(CHAIN_ID)
    assert ex.journal.entries()[0]["txn_hash"] == HASH and world.posts == []
    await close(ex)


@pytest.mark.asyncio
async def test_trace_completely_absent_keeps_previous_behavior(tmp_path):
    world, ex, op, log = await prepared(tmp_path)
    for field in ("output", "outputRaw"):
        del log[field]["executedCall"]
    assert await ex.send(envelope("erc20_mint")) == HASH
    assert proof(ex, op)["redactions"] == [] and world.posts == []
    await close(ex)


@pytest.mark.asyncio
async def test_full_output_without_raw_keeps_previous_behavior(tmp_path):
    world, ex, op, log = await prepared(tmp_path)
    log["output"] = log.pop("outputRaw")
    assert await ex.send(envelope("erc20_mint")) == HASH
    assert proof(ex, op)["redactions"] == [] and world.posts == []
    await close(ex)


@pytest.mark.asyncio
async def test_redacted_trace_without_full_witness_refuses(tmp_path):
    world, ex, op, log = await prepared(tmp_path)
    del log["outputRaw"]
    await refused(world, ex, op, tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, False, [], "malformed", {}])
async def test_malformed_present_trace_is_not_absence(tmp_path, bad):
    world, ex, op, log = await prepared(tmp_path)
    log["output"]["executedCall"] = bad
    await refused(world, ex, op, tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["plain_address", "mask", "raw_wrong_same_suffix", "foreign_execution",
                                    "foreign_hash", "missing_log", "missing_raw_field", "trace_disappeared",
                                    "trace_conflict", "mask_on_nonsensitive_path"])
async def test_corruption_refuses_without_journal_transition(tmp_path, change):
    world, ex, op, log = await prepared(tmp_path)
    if change == "plain_address":
        log["output"]["executedCall"]["args"]["to"] = STRANGER
    elif change == "mask":
        log["output"]["executedCall"]["args"]["token"] = "***" + USDC[-4:]
    elif change == "raw_wrong_same_suffix":
        log["outputRaw"]["executedCall"]["args"]["token"] = "0x" + "11" * 18 + USDC[-4:]
        assert logs_output(log["outputRaw"]) == log["output"]
    elif change == "foreign_execution":
        log["executionId"] = "other"
    elif change == "foreign_hash":
        for field in ("output", "outputRaw"):
            log[field]["transactionHash"] = "0x" + "bb" * 32
    elif change == "missing_log":
        world.receipts[HASH]["logs"] = []
    elif change == "missing_raw_field":
        del log["outputRaw"]["executedCall"]["args"]["token"]
    elif change == "trace_disappeared":
        del log["output"]["executedCall"]
    elif change == "trace_conflict":
        log["output"]["executedCall"]["reverted"] = True
    else:
        log["output"]["executedCall"]["contractAddress"] = "********" + FAUCET[-4:]
    await refused(world, ex, op, tmp_path)


def test_pinned_rule_branches_patterns_case_depth_and_utf16():
    assert redact_sensitive_data({"token": ""}) == {"token": "[REDACTED]"}
    assert redact_sensitive_data({"token": "1234"}) == {"token": "****"}
    assert redact_sensitive_data({"token": "12345"}) == {"token": "*2345"}
    assert redact_sensitive_data({"x-credential-y": "123456"}) == {"x-credential-y": "**3456"}
    assert redact_sensitive_data({"authThing": 1}) == {"authThing": "[REDACTED]"}
    assert redact_sensitive_data({"privateKey": "abcdef"}) == {"privateKey": "abcdef"}
    assert redact_sensitive_data({"token": "😀😀😀"}) == {"token": "**😀😀"}
    obj = {"token": "abcdef"}
    for _ in range(11):
        obj = {"child": obj}
    assert redact_sensitive_data(obj) == obj
