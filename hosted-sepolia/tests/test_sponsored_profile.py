"""The sponsored profile: the effective call, corroborated from the chain."""

import pytest

from hosted_sepolia.verify import (
    EffectExpectation,
    verify_sponsored_effect,
)
from tests.fixtures import (
    AMOUNT,
    AUSDC,
    CHAIN_ID,
    HASH,
    POOL,
    RELAYER,
    STRANGER,
    USDC,
    WALLET,
    WRAPPER,
    ZERO,
    executed_call_supply,
    sponsored_supply_receipt,
    sponsored_tx,
    supply_log,
    transfer_log,
    withdraw_log,
)

SUPPLY = EffectExpectation(
    kind="supply",
    chain_id=CHAIN_ID,
    target=POOL,
    wallet=WALLET,
    asset=USDC,
    amount=AMOUNT,
    a_token=AUSDC,
    referral_code=0,
)


def run(receipt=None, tx=None, expectation=SUPPLY, executed_call=None, chain=CHAIN_ID, fn="supply"):
    return verify_sponsored_effect(
        tx if tx is not None else sponsored_tx(),
        receipt if receipt is not None else sponsored_supply_receipt(),
        expectation,
        rpc_chain_id=chain,
        txn_hash=HASH,
        wallet_address=WALLET,
        executed_call=executed_call,
        expected_function=fn,
    )


# --- the positive case -----------------------------------------------------


def test_sponsored_supply_verifies_from_logs_alone():
    r = run()
    assert r.ok, r.blockers
    assert r.profile == "sponsored"
    assert "chain: receipt event logs" in r.evidence_origins


def test_executed_call_when_present_is_a_third_source():
    r = run(executed_call=executed_call_supply())
    assert r.ok, r.blockers
    assert "keeperhub: executedCall (trace)" in r.evidence_origins


def test_absent_executed_call_is_recorded_not_fatal():
    r = run(executed_call=None)
    assert r.ok
    assert any("no trace corroboration" in c for c in r.caveats)


def test_result_always_carries_the_exclusivity_caveat():
    r = run()
    assert any("did not prove" in c or "not prove" in c for c in r.caveats)
    assert any("outer receipt" in c for c in r.caveats)


# --- the ownership invariant ----------------------------------------------


def test_onbehalfof_is_the_ownership_invariant():
    # The position must be ours even though the relayer sent the transaction.
    bad = sponsored_supply_receipt(
        logs=[
            transfer_log(emitter=USDC, sender=WALLET, to=AUSDC, value=AMOUNT, index=0),
            transfer_log(emitter=AUSDC, sender=ZERO, to=STRANGER, value=AMOUNT, index=1),
            supply_log(on_behalf_of=STRANGER, index=2),
        ]
    )
    r = run(receipt=bad)
    assert not r.ok
    assert "Supply.onBehalfOf == our wallet" in {c.name for c in r.failed}


def test_supply_user_must_be_our_wallet():
    # Someone else's tokens funding our position is not our operation.
    bad = sponsored_supply_receipt(
        logs=[
            transfer_log(emitter=USDC, sender=WALLET, to=AUSDC, value=AMOUNT, index=0),
            transfer_log(emitter=AUSDC, sender=ZERO, to=WALLET, value=AMOUNT, index=1),
            supply_log(user=STRANGER, index=2),
        ]
    )
    r = run(receipt=bad)
    assert not r.ok
    assert "Supply.user == our wallet" in {c.name for c in r.failed}


# --- each bound field is load-bearing -------------------------------------


@pytest.mark.parametrize(
    "log_over,failing",
    [
        ({"emitter": STRANGER}, "log.Supply emitted by the target pool"),
        ({"reserve": AUSDC}, "Supply.reserve == asset"),
        ({"amount": AMOUNT + 1}, "Supply.amount"),
        ({"referral": 7}, "Supply.referralCode"),
    ],
    ids=["wrong emitter", "wrong reserve", "wrong amount", "wrong referral"],
)
def test_each_supply_field_is_checked(log_over, failing):
    logs = [
        transfer_log(emitter=USDC, sender=WALLET, to=AUSDC, value=AMOUNT, index=0),
        transfer_log(emitter=AUSDC, sender=ZERO, to=WALLET, value=AMOUNT, index=1),
        supply_log(index=2, **log_over),
    ]
    r = run(receipt=sponsored_supply_receipt(logs=logs))
    assert not r.ok
    assert failing in {c.name for c in r.failed}


def test_a_supply_event_from_an_impostor_contract_is_refused():
    # Anyone can deploy a contract that emits an identically shaped log.
    logs = [supply_log(emitter=STRANGER, index=0)]
    r = run(receipt=sponsored_supply_receipt(logs=logs))
    assert not r.ok


def test_no_supply_event_at_all_is_refused():
    r = run(receipt=sponsored_supply_receipt(logs=[]))
    assert not r.ok
    assert "log.Supply emitted by the target pool" in {c.name for c in r.failed}


def test_two_supplies_from_the_target_cannot_be_attributed():
    logs = [
        transfer_log(emitter=USDC, sender=WALLET, to=AUSDC, value=AMOUNT, index=0),
        transfer_log(emitter=AUSDC, sender=ZERO, to=WALLET, value=AMOUNT, index=1),
        supply_log(index=2),
        supply_log(index=3),
    ]
    r = run(receipt=sponsored_supply_receipt(logs=logs))
    assert not r.ok


def test_underlying_must_leave_our_wallet():
    logs = [
        transfer_log(emitter=USDC, sender=STRANGER, to=AUSDC, value=AMOUNT, index=0),
        transfer_log(emitter=AUSDC, sender=ZERO, to=WALLET, value=AMOUNT, index=1),
        supply_log(index=2),
    ]
    r = run(receipt=sponsored_supply_receipt(logs=logs))
    assert not r.ok
    assert "underlying Transfer(wallet -> aToken, amount)" in {c.name for c in r.failed}


def test_atoken_mint_allows_accrued_interest_but_not_less():
    more = [
        transfer_log(emitter=USDC, sender=WALLET, to=AUSDC, value=AMOUNT, index=0),
        transfer_log(emitter=AUSDC, sender=ZERO, to=WALLET, value=AMOUNT + 5, index=1),
        supply_log(index=2),
    ]
    assert run(receipt=sponsored_supply_receipt(logs=more)).ok

    less = [
        transfer_log(emitter=USDC, sender=WALLET, to=AUSDC, value=AMOUNT, index=0),
        transfer_log(emitter=AUSDC, sender=ZERO, to=WALLET, value=AMOUNT - 1, index=1),
        supply_log(index=2),
    ]
    r = run(receipt=sponsored_supply_receipt(logs=less))
    assert not r.ok


# --- receipt-level guards --------------------------------------------------


def test_reverted_outer_receipt_is_refused():
    r = run(receipt=sponsored_supply_receipt(status="0x0"))
    assert not r.ok
    assert "receipt.status == 1" in {c.name for c in r.failed}


def test_receipt_for_a_different_hash_is_refused():
    r = run(receipt=sponsored_supply_receipt(transactionHash="0x" + "ff" * 32))
    assert not r.ok


def test_wrong_rpc_chain_is_refused_even_with_perfect_logs():
    r = run(chain=8453)
    assert not r.ok
    assert "rpc_chain_id == authorized chain" in {c.name for c in r.failed}


def test_missing_receipt_is_refused():
    r = run(receipt=None if False else None)  # explicit: no receipt
    r = verify_sponsored_effect(
        sponsored_tx(), None, SUPPLY, rpc_chain_id=CHAIN_ID, txn_hash=HASH, wallet_address=WALLET
    )
    assert not r.ok
    assert "receipt_present" in {c.name for c in r.failed}


def test_a_direct_send_is_refused_by_the_sponsored_profile():
    # If our wallet is the top-level sender this is a direct send and must be
    # verified with the stronger profile. Accepting it here would let the
    # weaker check stand in for the stronger one.
    r = run(tx=sponsored_tx(**{"from": WALLET, "to": POOL}))
    assert not r.ok
    names = {c.name for c in r.failed}
    assert "top-level sender is not our wallet (a relayer)" in names
    assert "top-level recipient is the wrapper, not the target" in names


# --- executedCall corroboration -------------------------------------------


@pytest.mark.parametrize(
    "over,failing",
    [
        ({"contractAddress": STRANGER}, "executedCall.contractAddress == target"),
        ({"functionName": "withdraw"}, "executedCall.functionName"),
        ({"reverted": True}, "executedCall.reverted is False"),
        ({"sponsored": False}, "executedCall.sponsored is True"),
        ({"topLevelTo": POOL.lower()}, "executedCall.topLevelTo != target"),
    ],
    ids=["wrong target", "wrong function", "inner reverted", "not sponsored", "topLevelTo == target"],
)
def test_executed_call_disagreement_is_fatal(over, failing):
    r = run(executed_call=executed_call_supply(**over))
    assert not r.ok
    assert failing in {c.name for c in r.failed}


def test_executed_call_args_are_compared_against_what_we_authorized():
    bad = executed_call_supply(args={"asset": USDC, "amount": str(AMOUNT + 1), "onBehalfOf": WALLET})
    r = run(executed_call=bad)
    assert not r.ok
    assert any("amount" in c.name for c in r.failed)


def test_executed_call_onbehalfof_is_compared():
    bad = executed_call_supply(args={"asset": USDC, "amount": str(AMOUNT), "onBehalfOf": STRANGER})
    r = run(executed_call=bad)
    assert not r.ok


def test_a_perfect_executed_call_cannot_rescue_absent_logs():
    # The whole independence rule in one test: KeeperHub saying the right thing
    # must never substitute for the chain showing it.
    r = run(receipt=sponsored_supply_receipt(logs=[]), executed_call=executed_call_supply())
    assert not r.ok
    assert "log.Supply emitted by the target pool" in {c.name for c in r.failed}


# --- withdraw --------------------------------------------------------------

WITHDRAW = EffectExpectation(
    kind="withdraw", chain_id=CHAIN_ID, target=POOL, wallet=WALLET, asset=USDC, amount=AMOUNT, recipient=WALLET
)


def withdraw_receipt(**over):
    logs = over.pop("logs", [withdraw_log(index=0)])
    return sponsored_supply_receipt(logs=logs, **over)


def test_withdraw_verifies():
    r = run(receipt=withdraw_receipt(), expectation=WITHDRAW, fn="withdraw")
    assert r.ok, r.blockers


def test_withdraw_to_a_stranger_is_refused():
    r = run(receipt=withdraw_receipt(logs=[withdraw_log(to=STRANGER)]), expectation=WITHDRAW, fn="withdraw")
    assert not r.ok
    assert "Withdraw.to == recipient" in {c.name for c in r.failed}


def test_withdraw_user_must_be_our_wallet():
    r = run(receipt=withdraw_receipt(logs=[withdraw_log(user=STRANGER)]), expectation=WITHDRAW, fn="withdraw")
    assert not r.ok


def test_withdraw_all_uses_a_sentinel_and_cannot_be_amount_matched():
    from hosted_sepolia.verify import MAX_UINT256

    all_out = EffectExpectation(
        kind="withdraw",
        chain_id=CHAIN_ID,
        target=POOL,
        wallet=WALLET,
        asset=USDC,
        amount=MAX_UINT256,
        recipient=WALLET,
        exact_amount=False,
    )
    r = run(receipt=withdraw_receipt(logs=[withdraw_log(amount=987_654)]), expectation=all_out, fn="withdraw")
    assert r.ok, r.blockers
    note = next(c for c in r.checks if c.name == "Withdraw.amount")
    assert "sentinel" in note.detail


def test_withdraw_all_still_refuses_a_zero_amount():
    from hosted_sepolia.verify import MAX_UINT256

    all_out = EffectExpectation(
        kind="withdraw", chain_id=CHAIN_ID, target=POOL, wallet=WALLET, asset=USDC,
        amount=MAX_UINT256, recipient=WALLET, exact_amount=False,
    )
    r = run(receipt=withdraw_receipt(logs=[withdraw_log(amount=0)]), expectation=all_out, fn="withdraw")
    assert not r.ok


# --- approve and faucet mint ----------------------------------------------


def test_approve_binds_owner_spender_and_value():
    from tests.fixtures import approval_log

    exp = EffectExpectation(kind="approve", chain_id=CHAIN_ID, target=USDC, wallet=WALLET, spender=POOL, amount=AMOUNT)
    ok = run(receipt=sponsored_supply_receipt(logs=[approval_log()]), expectation=exp, fn="approve")
    assert ok.ok, ok.blockers

    bad = run(receipt=sponsored_supply_receipt(logs=[approval_log(spender=STRANGER)]), expectation=exp, fn="approve")
    assert not bad.ok
    assert "Approval.spender" in {c.name for c in bad.failed}

    bad_owner = run(receipt=sponsored_supply_receipt(logs=[approval_log(owner=STRANGER)]), expectation=exp, fn="approve")
    assert not bad_owner.ok


def test_faucet_mint_binds_token_recipient_and_amount():
    exp = EffectExpectation(kind="erc20_mint", chain_id=CHAIN_ID, target=USDC, wallet=WALLET, asset=USDC, amount=AMOUNT)
    logs = [transfer_log(emitter=USDC, sender=ZERO, to=WALLET, value=AMOUNT)]
    assert run(receipt=sponsored_supply_receipt(logs=logs), expectation=exp, fn="mint").ok

    wrong = [transfer_log(emitter=USDC, sender=ZERO, to=STRANGER, value=AMOUNT)]
    assert not run(receipt=sponsored_supply_receipt(logs=wrong), expectation=exp, fn="mint").ok


def test_an_unknown_effect_kind_is_refused_rather_than_assumed():
    exp = EffectExpectation(kind="flash_loan", chain_id=CHAIN_ID, target=POOL, wallet=WALLET)
    r = run(expectation=exp)
    assert not r.ok
    assert any("no effect matcher" in b for b in r.blockers)
