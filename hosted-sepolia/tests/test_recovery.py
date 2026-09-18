"""Real SDK journal/seam + executor + HTTP parsing; RPC/HTTP effects are models.

The model evolves supply/withdraw position and retains it across client restart.
It cannot witness Turnkey, EVM runtime, or hosted durable writes/fencing.
"""

import json
from copy import deepcopy

import httpx
import pytest
from eth_abi import encode
from eth_utils import function_signature_to_4byte_selector

from keeperhub_executor.client import KeeperHubClient
from keeperhub_executor.workflow import ACTION_NODE_ID
from hosted_sepolia.recovery import (
    build_execution,
    CALLS,
    VerifiedJournal,
    expectation_from_envelope,
)
from wayfinder_paths.core.utils.executor import (
    ExecutionOutcomeUnknownError,
    build_envelope,
)
from tests.fixtures import (
    AMOUNT,
    CHAIN_ID,
    HASH,
    POOL,
    STRANGER,
    USDC,
    WALLET,
    sponsored_supply_receipt,
    sponsored_tx,
    withdraw_log,
    supply_log,
    executed_call_supply,
)


def envelope(kind="supply", amount=AMOUNT):
    sig, (_, target, types) = next((s, v) for s, v in CALLS.items() if v[0] == kind)
    args = {
        "supply": [USDC, amount, WALLET, 0],
        "withdraw": [USDC, amount, WALLET],
        "approve": [POOL, amount],
        "erc20_mint": [USDC, WALLET, amount],
    }[kind]
    return build_envelope(
        {
            "chainId": CHAIN_ID,
            "from": WALLET,
            "to": target,
            "value": 0,
            "data": "0x"
            + function_signature_to_4byte_selector(sig).hex()
            + encode(types, args).hex(),
        }
    )


def abi_resolver(chain, to, selector):
    for sig, (kind, target, types) in CALLS.items():
        if "0x" + function_signature_to_4byte_selector(sig).hex() == selector:
            names = {
                "supply": ["asset", "amount", "onBehalfOf", "referralCode"],
                "withdraw": ["asset", "amount", "to"],
                "approve": ["spender", "amount"],
                "erc20_mint": ["token", "to", "amount"],
            }[kind]
            return [
                {
                    "type": "function",
                    "name": sig.split("(")[0],
                    "stateMutability": "nonpayable",
                    "inputs": [{"name": n, "type": t} for n, t in zip(names, types)],
                    "outputs": [],
                }
            ]


class World:
    def __init__(self):
        self.rows = []
        self.logs = {}
        self.txs = {}
        self.receipts = {}
        self.posts = []
        self.lose_response = False
        self.hidden = False
        self.position = 0
        self.balance = AMOUNT
        self.initial_balance = self.balance
        self.journal_path = None

    def http(self, request):
        if request.method == "POST":
            body = json.loads(request.content)["input"]
            op = body["operationId"]
            # Real SQLite read at the HTTP boundary establishes before-submit durability.
            j = VerifiedJournal(self.journal_path)
            assert j.authorized(op)["state"] == "pending"
            j.close()
            assert request.headers["Idempotency-Key"] == op
            assert op not in self.posts
            self.posts.append(op)
            idx = len(self.posts)
            h = "0x" + f"{idx:064x}"
            ex = "exec-" + str(idx)
            row = {
                "id": ex,
                "workflowId": "wf",
                "status": "success",
                "input": body,
                "transactionHashes": [
                    {"nodeId": ACTION_NODE_ID, "chainId": CHAIN_ID, "hash": h}
                ],
            }
            out = {
                "success": True,
                "sponsored": True,
                "transactionHash": h,
                "chainId": CHAIN_ID,
            }
            self.rows.append(row)
            self.logs[ex] = {
                "execution": deepcopy(row),
                "logs": [
                    {
                        "executionId": ex,
                        "nodeId": ACTION_NODE_ID,
                        "nodeType": "web3/write-contract",
                        "status": "success",
                        "output": out,
                        "outputRaw": deepcopy(out),
                    }
                ]
            }
            kind = body["abiFunction"]
            amount = int(body["functionArgs"][1])
            if kind == "supply":
                assert self.balance >= amount
                self.balance -= amount
                self.position += amount
                receipt = sponsored_supply_receipt(transactionHash=h)
            elif kind == "withdraw":
                assert self.position >= amount
                self.position -= amount
                self.balance += amount
                receipt = sponsored_supply_receipt(
                    transactionHash=h, logs=[withdraw_log(amount=amount)]
                )
            else:
                raise AssertionError("model only implements supply/withdraw")
            self.txs[h] = sponsored_tx(hash=h)
            self.receipts[h] = receipt
            if self.lose_response:
                self.lose_response = False
                raise httpx.ReadError(
                    "modeled response loss after server acceptance", request=request
                )
            return httpx.Response(200, json={"executionId": ex})
        if request.url.path == "/api/workflows/wf/executions":
            return httpx.Response(200, json=[] if self.hidden else self.rows)
        for ex, logs in self.logs.items():
            if request.url.path == f"/api/workflows/executions/{ex}/logs":
                return httpx.Response(200, json=logs)
        raise AssertionError("unexpected HTTP " + str(request.url))

    async def rpc(self, method, params):
        if method == "eth_chainId":
            return hex(CHAIN_ID)
        if method == "eth_getTransactionByHash":
            return deepcopy(self.txs.get(params[0]))
        if method == "eth_getTransactionReceipt":
            return deepcopy(self.receipts.get(params[0]))
        raise AssertionError("unexpected RPC " + method)

    async def open(self, tmp_path, *, allow=True):
        client = KeeperHubClient("https://offline.invalid", "fixture")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(self.http))
        self.journal_path = tmp_path / "journal.sqlite"
        execution = build_execution(
            journal_path=self.journal_path,
            client=client,
            workflow_id="wf",
            wallet_address=WALLET,
            rpc_url="https://offline.invalid",
            abi_resolver=abi_resolver,
            allow_submit=allow,
            submit_timeout=0.02,
            poll_interval=0,
        )
        execution.executor.reconciler.rpc = self.rpc
        return execution


async def close(execution):
    execution.journal.close()
    await execution.executor.close()
    await execution.executor.client.close()


@pytest.mark.asyncio
async def test_response_loss_resume_same_id_then_withdraw(tmp_path):
    world = World()
    ex = await world.open(tmp_path)
    world.lose_response = True
    with pytest.raises(ExecutionOutcomeUnknownError):
        await ex.send(envelope())
    row = ex.journal.entries()[0]
    op = row["operation_id"]
    assert row["state"] == "pending"
    assert world.position == AMOUNT and len(world.posts) == 1
    await close(ex)
    ex = await world.open(tmp_path)
    h = await ex.send(envelope())
    assert len(world.posts) == 1 and ex.journal.entries()[0]["operation_id"] == op
    assert ex.journal.entries()[0]["consumed"] is True
    proof = json.loads(
        ex.journal._conn.execute(
            "SELECT proof FROM hosted_verification WHERE operation_id=?", (op,)
        ).fetchone()[0]
    )
    assert (
        proof["ok"]
        and proof["goalVerified"] is False
        and "not exclusive" in proof["limitations"]
    )
    # Separate model position check, not inferred from receipt status or journal state.
    assert world.position == AMOUNT and world.balance == 0
    h2 = await ex.send(envelope("withdraw"))
    assert h2 != h and len(world.posts) == 2
    assert world.position == 0 and world.balance == world.initial_balance
    assert len(ex.journal.entries()) == 2
    await close(ex)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "empty_listing",
        "hash_conflict",
        "mode_conflict",
        "wrong_trace",
        "wrong_amount",
        "absent_tx",
        "reverted_receipt",
        "missing_events",
        "wrong_record",
        "authorization_drift",
    ],
)
async def test_uncertainty_keeps_original_operation_and_never_resubmits(
    tmp_path, corruption
):
    w = World()
    ex = await w.open(tmp_path)
    w.lose_response = True
    with pytest.raises(ExecutionOutcomeUnknownError):
        await ex.send(envelope())
    original = deepcopy(ex.journal.entries())
    h = next(iter(w.txs))
    lg = w.logs["exec-1"]["logs"][0]
    if corruption == "empty_listing":
        w.hidden = True
    elif corruption == "hash_conflict":
        lg["outputRaw"]["transactionHash"] = "0x" + "ff" * 32
    elif corruption == "mode_conflict":
        lg["outputRaw"]["sponsored"] = False
    elif corruption == "wrong_trace":
        lg["outputRaw"]["executedCall"] = executed_call_supply(topLevelTo=STRANGER)
    elif corruption == "wrong_amount":
        w.receipts[h]["logs"][-1] = supply_log(amount=100 * AMOUNT)
    elif corruption == "absent_tx":
        w.txs.clear()
    elif corruption == "reverted_receipt":
        w.receipts[h]["status"] = "0x0"
    elif corruption == "missing_events":
        w.receipts[h]["logs"] = []
    elif corruption == "wrong_record":
        w.rows[0]["input"]["functionArgs"][1] = str(AMOUNT + 1)
    else:
        ex.journal._conn.execute("UPDATE operations SET digest=?", ("bad",))
        original = deepcopy(ex.journal.entries())
    for _ in range(2):
        with pytest.raises(ExecutionOutcomeUnknownError):
            await ex.send(envelope())
        assert ex.journal.entries() == original
    assert len(w.posts) == 1
    await close(ex)


@pytest.mark.asyncio
async def test_resume_cannot_skip_interrupted_supply(tmp_path):
    w = World()
    ex = await w.open(tmp_path)
    w.lose_response = True
    with pytest.raises(ExecutionOutcomeUnknownError):
        await ex.send(envelope())
    with pytest.raises(ExecutionOutcomeUnknownError):
        await ex.send(envelope("withdraw"))
    assert len(w.posts) == 1 and ex.journal.entries()[0]["state"] == "pending"
    await close(ex)


def test_transition_needs_proof_and_rechecks_authorization(tmp_path):
    j = VerifiedJournal(tmp_path / "j.sqlite")
    op = j.begin(envelope())
    with pytest.raises(ValueError, match="no matching"):
        j.record_hash(op, HASH, consumed=False)
    j.save_proof(
        op, HASH, j.authorized(op)["digest"], {"ok": True, "meaning": "test fixture"}
    )
    j._conn.execute("UPDATE operations SET digest=?", ("changed",))
    with pytest.raises(ValueError, match="inconsistent"):
        j.record_hash(op, HASH, consumed=False)
    assert j.entries()[0]["state"] == "pending"
    j.close()


@pytest.mark.asyncio
async def test_submit_disabled_by_default(tmp_path):
    w = World()
    ex = await w.open(tmp_path, allow=False)
    with pytest.raises(ExecutionOutcomeUnknownError):
        await ex.send(envelope())
    assert w.posts == []
    await close(ex)


@pytest.mark.parametrize(
    "field,value",
    [
        ("chainId", 8453),
        ("from", STRANGER),
        ("to", STRANGER),
        ("data", "0xdeadbeef"),
        ("value", 1),
    ],
)
def test_authorized_envelope_constructor_checks_all_fields(field, value):
    env = envelope()
    env[field] = value
    with pytest.raises(ValueError):
        expectation_from_envelope(env, WALLET)


def test_only_decoded_max_authorizes_variable_withdraw():
    assert (
        expectation_from_envelope(envelope("withdraw", 2**256 - 1), WALLET).exact_amount
        is False
    )
    assert expectation_from_envelope(envelope("withdraw"), WALLET).exact_amount is True


@pytest.mark.asyncio
async def test_sdk_transaction_path_does_not_fallback_or_resend_on_unknown(
    tmp_path, monkeypatch
):
    from wayfinder_paths.core.utils import transaction as sdk_tx

    w = World()
    ex = await w.open(tmp_path)
    w.lose_response = True

    async def forbidden(*args, **kwargs):
        pytest.fail(
            "SDK must not sign, fund, resolve nonce, sponsor or broadcast locally"
        )

    async def callback(*args, **kwargs):
        await forbidden()

    callback.external_execution = ex
    callback.wallet_address = WALLET
    for name in [
        "gas_limit_transaction",
        "nonce_transaction",
        "gas_price_transaction",
        "broadcast_transaction",
        "send_sponsored_transaction",
        "sponsorship_enabled",
    ]:
        monkeypatch.setattr(sdk_tx, name, forbidden)
    with pytest.raises(ExecutionOutcomeUnknownError):
        await sdk_tx.send_transaction(envelope(), callback, wait_for_receipt=False)
    # Using the same preserved world and journal, actual SDK retry claims the hash.
    result = await sdk_tx.send_transaction(envelope(), callback, wait_for_receipt=False)
    assert len(w.posts) == 1 and result
    await close(ex)


@pytest.mark.asyncio
async def test_unconsumed_hash_rechecked_on_resume(tmp_path):
    w = World()
    ex = await w.open(tmp_path)
    w.lose_response = True
    with pytest.raises(ExecutionOutcomeUnknownError):
        await ex.send(envelope())
    await ex.resolve_pending(CHAIN_ID)
    row = ex.journal.entries()[0]
    assert row["state"] == "submitted" and not row["consumed"]
    w.receipts[row["txn_hash"]]["logs"] = []
    with pytest.raises(ExecutionOutcomeUnknownError):
        await ex.send(envelope())
    assert ex.journal.entries()[0] == row and len(w.posts) == 1
    await close(ex)


@pytest.mark.asyncio
async def test_authorization_changed_during_rpc_refused(tmp_path):
    w = World()
    ex = await w.open(tmp_path)
    w.lose_response = True
    with pytest.raises(ExecutionOutcomeUnknownError):
        await ex.send(envelope())

    async def drift(method, params):
        value = await w.rpc(method, params)
        if method == "eth_getTransactionReceipt":
            ex.journal._conn.execute(
                "UPDATE operations SET digest=?", ("changed-during-rpc",)
            )
        return value

    ex.executor.reconciler.rpc = drift
    with pytest.raises(ExecutionOutcomeUnknownError):
        await ex.send(envelope())
    assert ex.journal.entries()[0]["state"] == "pending" and len(w.posts) == 1
    await close(ex)


@pytest.mark.asyncio
async def test_recovery_command_applies_only_after_fresh_verification(tmp_path):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "scripts/reconcile_hosted.py"
    spec = importlib.util.spec_from_file_location("reconcile_command", path)
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    w = World()
    ex = await w.open(tmp_path)
    w.lose_response = True
    with pytest.raises(ExecutionOutcomeUnknownError):
        await ex.send(envelope())
    result = await command.reconcile(ex)
    assert result["observations"][0]["outcome"] == "landed"
    assert ex.journal.entries()[0]["state"] == "pending"
    w.receipts[next(iter(w.receipts))]["logs"] = []
    with pytest.raises(ExecutionOutcomeUnknownError):
        await command.reconcile(ex, apply=True)
    assert ex.journal.entries()[0]["state"] == "pending" and len(w.posts) == 1
    await close(ex)


@pytest.mark.asyncio
async def test_reverted_hash_and_ownership_remain_available_without_acceptance(tmp_path):
    w = World()
    execution = await w.open(tmp_path)
    w.lose_response = True
    with pytest.raises(ExecutionOutcomeUnknownError):
        await execution.send(envelope())
    h = next(iter(w.txs))
    w.receipts[h]['status'] = '0x0'
    with pytest.raises(ExecutionOutcomeUnknownError):
        await execution.resolve_pending(CHAIN_ID)
    row = execution.journal.entries()[0]
    assert row['state'] == 'pending' and row['txn_hash'] is None
    observations = execution.journal.observations(row['operation_id'])
    assert len(observations) == 1
    assert observations[0]['txnHash'] == h and observations[0]['receipt']['status'] == '0x0'
    assert observations[0]['ownership']['operationId'] == row['operation_id']
    assert observations[0]['accepted'] is False
    assert len(w.posts) == 1
    await close(execution)
