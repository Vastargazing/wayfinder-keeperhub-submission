"""Synthetic regressions for the independent review; no signer or network."""

from dataclasses import replace
from itertools import combinations

import pytest

from hosted_sepolia.verify import (
    EffectExpectation,
    ExecutionMode,
    classify_execution_mode,
    verify_sponsored_effect,
)
from tests.fixtures import (
    AMOUNT,
    AUSDC,
    CHAIN_ID,
    HASH,
    POOL,
    STRANGER,
    USDC,
    WALLET,
    ZERO,
    SUPPLY_T0,
    sponsored_supply_receipt,
    sponsored_tx,
    transfer_log,
    supply_log,
    withdraw_log,
    approval_log,
    executed_call_supply,
    word_addr,
    word_uint,
)

WANT = EffectExpectation(
    kind="supply",
    chain_id=CHAIN_ID,
    target=POOL,
    wallet=WALLET,
    asset=USDC,
    amount=AMOUNT,
    a_token=AUSDC,
    referral_code=0,
)


def check(tx=None, *, missing=False, receipt=None, want=WANT, ec=None):
    return verify_sponsored_effect(
        None if missing else (sponsored_tx() if tx is None else tx),
        receipt if receipt is not None else sponsored_supply_receipt(),
        want,
        rpc_chain_id=CHAIN_ID,
        txn_hash=HASH,
        wallet_address=WALLET,
        executed_call=ec,
        expected_function=want.kind,
    )


def test_missing_transaction():
    assert not check(missing=True).ok


@pytest.mark.parametrize(
    "field,value", [("hash", "0x" + "ff" * 32), ("chainId", hex(8453))]
)
def test_transaction_identity(field, value):
    assert not check(sponsored_tx(**{field: value})).ok


@pytest.mark.parametrize("field", ["hash", "chainId", "from", "to", "input", "value"])
def test_required_transaction_fields(field):
    tx = sponsored_tx()
    del tx[field]
    assert not check(tx).ok


def test_wallet_binding():
    receipt = sponsored_supply_receipt(
        logs=[
            transfer_log(emitter=USDC, sender=STRANGER, to=AUSDC, value=AMOUNT),
            transfer_log(emitter=AUSDC, sender=ZERO, to=STRANGER, value=AMOUNT),
            supply_log(user=STRANGER, on_behalf_of=STRANGER),
        ]
    )
    assert not check(receipt=receipt, want=replace(WANT, wallet=STRANGER)).ok


@pytest.mark.parametrize("amount,exact", [(AMOUNT, False), (None, True), (None, False)])
def test_no_arbitrary_withdraw_amount_relaxation(amount, exact):
    want = replace(
        WANT, kind="withdraw", amount=amount, exact_amount=exact, recipient=WALLET
    )
    assert not check(
        receipt=sponsored_supply_receipt(logs=[withdraw_log(amount=100 * AMOUNT)]),
        want=want,
    ).ok


@pytest.mark.parametrize("kind", ["supply", "erc20_mint", "approve"])
def test_missing_authorized_amount_rejected(kind):
    logs = {
        "supply": sponsored_supply_receipt()["logs"],
        "erc20_mint": [
            transfer_log(emitter=USDC, sender=ZERO, to=WALLET, value=AMOUNT)
        ],
        "approve": [approval_log(value=AMOUNT)],
    }[kind]
    assert not check(
        receipt=sponsored_supply_receipt(logs=logs),
        want=replace(WANT, kind=kind, amount=None),
    ).ok


@pytest.mark.parametrize(
    "field,value",
    [
        ("topLevelTo", STRANGER),
        ("referralCode", "77"),
        ("amount", "3"),
        ("onBehalfOf", STRANGER),
    ],
)
def test_executed_call_must_agree(field, value):
    ec = executed_call_supply()
    if field == "topLevelTo":
        ec[field] = value
    else:
        ec["args"][field] = value
    assert not check(ec=ec).ok


def test_max_withdraw_trace_checks_authorized_sentinel():
    want = replace(
        WANT, kind="withdraw", amount=2**256 - 1, exact_amount=False, recipient=WALLET
    )
    ec = executed_call_supply(
        functionName="withdraw",
        args={"asset": USDC, "amount": str(AMOUNT), "to": WALLET},
    )
    assert not check(
        want=want, ec=ec, receipt=sponsored_supply_receipt(logs=[withdraw_log()])
    ).ok


def test_valid_unrelated_log0():
    logs = sponsored_supply_receipt()["logs"] + [
        {"address": STRANGER, "topics": [], "data": "0x1234", "logIndex": "0x3"}
    ]
    assert check(receipt=sponsored_supply_receipt(logs=logs)).ok


@pytest.mark.parametrize(
    "field,value",
    [
        ("data", "0xzz"),
        ("address", "invalid"),
        ("logIndex", "0xzz"),
        ("topics", [SUPPLY_T0, "0x" + "zz" * 32, word_addr(WALLET), word_uint(0)]),
    ],
)
def test_malformed_relevant_logs_return_refusal(field, value):
    lg = supply_log()
    lg[field] = value
    assert not check(receipt=sponsored_supply_receipt(logs=[lg])).ok


@pytest.mark.parametrize(
    "field,value",
    [
        ("from", STRANGER),
        ("to", STRANGER),
        ("blockNumber", "0x12"),
        ("blockHash", "0x" + "cc" * 32),
    ],
)
def test_receipt_transaction_shared_fields_must_agree(field, value):
    tx = sponsored_tx(blockNumber="0x2c4b3c6", blockHash="0x" + "bb" * 32)
    receipt = (
        sponsored_supply_receipt(blockHash="0x" + "bb" * 32, **{field: value})
        if field != "blockHash"
        else sponsored_supply_receipt(blockHash=value)
    )
    assert not check(tx, receipt=receipt).ok


@pytest.mark.parametrize("a,b", list(combinations(range(4), 2)))
def test_conflicts_between_every_flag_source(a, b):
    out, raw = {}, {}
    for i, v in [(a, False), (b, True)]:
        obj = out if i < 2 else raw
        if i % 2:
            obj["executedCall"] = {"sponsored": v}
        else:
            obj["sponsored"] = v
    finding = classify_execution_mode(out, raw)
    assert finding.mode is ExecutionMode.UNKNOWN
    assert "conflict" in finding.detail.lower()


def test_trace_shape_is_not_sponsorship_evidence():
    for flag in (False, True):
        assert (
            classify_execution_mode({"executedCall": {"sponsored": flag}}).mode
            is ExecutionMode.UNKNOWN
        )


def test_no_unperformed_ownership_claim():
    result = check()
    assert result.ok
    assert not any(
        "hash bound to operation id" in s for s in result.evidence_origins
    )
