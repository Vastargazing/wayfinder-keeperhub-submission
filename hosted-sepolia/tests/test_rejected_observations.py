"""Attributed rejected candidates survive restart without advancing SDK state."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from keeperhub_executor.provenance import POLICY_VERSION
from tests.test_provenance import prepared
from tests.test_recovery import close, envelope
from tests.fixtures import CHAIN_ID, HASH, STRANGER, USDC
from wayfinder_paths.core.utils.executor import ExecutionOutcomeUnknownError


def corrupt(world, log, case):
    if case == "wrong_clear_address":
        log["output"]["executedCall"]["args"]["to"] = STRANGER
        return "/executedCall/args/to"
    log["output"]["executedCall"]["args"]["token"] = "***" + USDC[-4:]
    if case == "wrong_mask_reverted":
        world.receipts[HASH]["status"] = "0x0"
    return "/executedCall/args/token"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["wrong_mask", "wrong_mask_reverted", "wrong_clear_address"])
async def test_rejected_derivation_survives_reopen_and_resume(tmp_path, case):
    world, ex, op, log = await prepared(tmp_path)
    submit = AsyncMock(side_effect=AssertionError("resume must not enter submit"))
    ex.executor.submit = submit
    path = corrupt(world, log, case)
    original = deepcopy(ex.journal.entries())
    receipt = deepcopy(world.receipts[HASH])
    result = await ex.executor.lookup(op)
    assert result.outcome.value == "indeterminate"
    observations = ex.journal.observations(op)
    assert len(observations) == 1
    observation = observations[0]
    assert observation["operationId"] == op and observation["txnHash"] == HASH
    assert observation["receipt"] == receipt
    assert observation["accepted"] is False
    verification = observation["verification"]
    assert verification["ok"] is False and verification["policyVersion"] == POLICY_VERSION
    assert verification["provenance"]["records"]
    assert verification["ownership"]["executionId"] == "exec-1"
    assert any(path in blocker for blocker in verification["blockers"])
    # Rejected trace must not become an absent/optional successful trace check.
    assert verification["results"][0]["stage"] == "trace_derivation"
    assert verification["results"][0]["ok"] is False
    assert "absent" not in str(verification["results"])
    for _ in range(2):
        assert ex.journal.entries() == original
        assert ex.journal._conn.execute(
            "SELECT count(*) FROM hosted_verification WHERE operation_id=?", (op,)
        ).fetchone()[0] == 0
        assert world.posts == [] and ex.executor.submits == 0
        submit.assert_not_called()
        await close(ex)
        ex = await world.open(tmp_path, allow=False)
        submit = AsyncMock(side_effect=AssertionError("resume must not enter submit"))
        ex.executor.submit = submit
        assert ex.journal.observations(op) == observations
        for resume in (lambda: ex.resolve_pending(CHAIN_ID), lambda: ex.send(envelope("erc20_mint"))):
            with pytest.raises(ExecutionOutcomeUnknownError):
                await resume()
            assert ex.journal.observations(op) == observations
            assert ex.journal.entries() == original
            assert world.posts == [] and ex.executor.submits == 0
            submit.assert_not_called()
    await close(ex)


@pytest.mark.asyncio
async def test_rejected_derivation_preserves_proof_but_revokes_permission(tmp_path):
    world, ex, op, log = await prepared(tmp_path)
    assert (await ex.executor.lookup(op)).outcome.value == "landed"
    assert ex.journal.observations(op)[0]["accepted"] is True
    original = deepcopy(ex.journal.entries())  # lookup has not adopted the hash
    corrupt(world, log, "wrong_mask")
    assert (await ex.executor.lookup(op)).outcome.value == "indeterminate"
    assert ex.journal.observations(op)[0]["accepted"] is False
    assert ex.journal._proof_row(op) is not None
    with pytest.raises(ValueError, match="no matching fresh verification permission"):
        ex.journal.record_hash(op, HASH, consumed=False)
    await close(ex)
    ex = await world.open(tmp_path, allow=False)
    assert ex.journal.observations(op)[0]["accepted"] is False
    assert ex.journal.entries() == original
    assert world.posts == [] and ex.executor.submits == 0
    await close(ex)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["foreign_execution", "foreign_hash"])
async def test_unbound_candidate_is_not_persisted(tmp_path, case):
    world, ex, op, log = await prepared(tmp_path)
    corrupt(world, log, "wrong_mask")
    original = deepcopy(ex.journal.entries())
    if case == "foreign_execution":
        log["executionId"] = "foreign"
    else:
        log["outputRaw"]["transactionHash"] = "0x" + "bb" * 32
    assert (await ex.executor.lookup(op)).outcome.value == "indeterminate"
    await close(ex)
    ex = await world.open(tmp_path, allow=False)
    assert ex.journal.observations(op) == []
    assert ex.journal.entries() == original
    assert world.posts == [] and ex.executor.submits == 0
    await close(ex)


@pytest.mark.asyncio
async def test_rejected_derivation_rechecks_authorization_before_persisting(tmp_path):
    world, ex, op, log = await prepared(tmp_path)
    corrupt(world, log, "wrong_mask")
    original_rpc = world.rpc

    async def changing_rpc(method, params):
        result = await original_rpc(method, params)
        if method == "eth_chainId":
            ex.journal._conn.execute("UPDATE operations SET digest='changed' WHERE operation_id=?", (op,))
        return result

    ex.executor.reconciler.rpc = changing_rpc
    assert (await ex.executor.lookup(op)).outcome.value == "indeterminate"
    await close(ex)
    ex = await world.open(tmp_path, allow=False)
    assert ex.journal.observations(op) == []
    row = ex.journal.entries()[0]
    assert row["state"] == "pending" and row["txn_hash"] is None and not row["consumed"]
    assert world.posts == [] and ex.executor.submits == 0
    await close(ex)
