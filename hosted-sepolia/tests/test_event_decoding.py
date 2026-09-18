"""Log decoding must refuse anything that only looks like the event."""

import pytest

from hosted_sepolia.events import (
    APPROVAL_TOPIC0,
    SUPPLY_TOPIC0,
    TRANSFER_TOPIC0,
    WITHDRAW_TOPIC0,
    MalformedLog,
    decode_log,
    decode_receipt_logs,
)
from tests.fixtures import (
    AMOUNT,
    POOL,
    USDC,
    WALLET,
    supply_log,
    withdraw_log,
    word_addr,
    word_uint,
)


def test_topic0_values_match_the_signatures_observed_on_chain():
    # Read off live Base Sepolia pool logs, evidence/11-event-signatures.json.
    assert SUPPLY_TOPIC0 == "0x2b627736bca15cd5381dcf80b0bf11fd197d01a037c52b927a881a10fb73ba61"
    assert WITHDRAW_TOPIC0 == "0x3115d1449a7b732c986cba18244e897a450f61e1bb8d589cd2e69e6c8924f9f7"
    assert TRANSFER_TOPIC0 == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    assert APPROVAL_TOPIC0 == "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"


def test_supply_indexed_layout_is_as_observed_not_as_guessed():
    # `user` is a DATA word and `onBehalfOf` is INDEXED. Swapping them would
    # compare the wrong address and still "pass" on the happy path.
    d = decode_log(supply_log(user=WALLET, on_behalf_of=POOL))
    assert d.fields["user"] == WALLET
    assert d.fields["onBehalfOf"] == POOL


def test_withdraw_has_three_indexed_fields_and_one_data_word():
    d = decode_log(withdraw_log())
    assert d.fields == {"reserve": USDC, "user": WALLET, "to": WALLET, "amount": AMOUNT}


def test_unknown_topic0_returns_none_rather_than_raising():
    assert decode_log({"address": POOL, "topics": ["0x" + "11" * 32], "data": "0x"}) is None


def test_dirty_address_padding_is_refused():
    log = supply_log()
    log["topics"][1] = "0x" + "ff" + "0" * 22 + USDC.lower()[2:]
    with pytest.raises(MalformedLog, match="padding"):
        decode_log(log)


def test_wrong_topic_count_for_a_claimed_event_is_refused():
    log = supply_log()
    log["topics"] = log["topics"][:3]
    with pytest.raises(MalformedLog, match="topics"):
        decode_log(log)


def test_wrong_data_word_count_is_refused():
    log = supply_log()
    log["data"] = word_uint(1)  # one word where Supply carries two
    with pytest.raises(MalformedLog, match="data words"):
        decode_log(log)


def test_ragged_data_is_refused():
    log = supply_log()
    log["data"] = "0x1234"
    with pytest.raises(MalformedLog, match="whole number of words"):
        decode_log(log)


def test_emitter_is_part_of_the_decoded_value():
    d = decode_log(supply_log(emitter=USDC))
    assert d.emitter == USDC


def test_receipt_without_logs_array_is_refused():
    with pytest.raises(MalformedLog):
        decode_receipt_logs({"status": "0x1"})


def test_unrecognised_logs_are_skipped_not_dropped_silently_from_known_ones():
    receipt = {"logs": [{"address": POOL, "topics": ["0x" + "22" * 32], "data": "0x"}, supply_log()]}
    out = decode_receipt_logs(receipt)
    assert len(out) == 1 and out[0].name == "Supply"
