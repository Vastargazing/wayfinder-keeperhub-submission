"""The direct profile must be exactly as strict as before."""

import pytest

from hosted_sepolia.verify import verify_direct_envelope
from tests.fixtures import CHAIN_ID, HASH, POOL, USDC, WALLET

ENVELOPE = {
    "chainId": CHAIN_ID,
    "from": WALLET,
    "to": POOL,
    "data": "0x617ba037" + "00" * 4,
    "value": 0,
}


def good_tx(**over):
    tx = {
        "hash": HASH,
        "from": WALLET,
        "to": POOL,
        "input": ENVELOPE["data"],
        "value": "0x0",
        "chainId": hex(CHAIN_ID),
    }
    tx.update(over)
    return tx


def test_matching_direct_transaction_passes():
    r = verify_direct_envelope(good_tx(), ENVELOPE, rpc_chain_id=CHAIN_ID, txn_hash=HASH)
    assert r.ok, r.blockers
    assert r.profile == "direct"


@pytest.mark.parametrize(
    "over",
    [
        {"from": "0x1111111111111111111111111111111111111111"},
        {"to": USDC},
        {"input": "0x617ba037" + "01" * 4},
        {"value": "0x1"},
        {"hash": "0x" + "cd" * 32},
        {"chainId": hex(8453)},
    ],
    ids=["wrong sender", "wrong recipient", "altered calldata", "altered value", "wrong hash", "wrong tx chain"],
)
def test_each_field_is_load_bearing(over):
    r = verify_direct_envelope(good_tx(**over), ENVELOPE, rpc_chain_id=CHAIN_ID, txn_hash=HASH)
    assert not r.ok
    assert r.failed


def test_wrong_rpc_chain_fails_even_when_the_transaction_matches():
    r = verify_direct_envelope(good_tx(), ENVELOPE, rpc_chain_id=8453, txn_hash=HASH)
    assert not r.ok


def test_missing_transaction_is_a_failure_not_an_absence_proof():
    r = verify_direct_envelope(None, ENVELOPE, rpc_chain_id=CHAIN_ID, txn_hash=HASH)
    assert not r.ok
    assert any("not found" in b for b in r.blockers)


def test_a_sponsored_transaction_fails_the_direct_profile():
    # This is the whole reason a second profile exists. It must keep failing.
    from tests.fixtures import RELAYER, WRAPPER

    r = verify_direct_envelope(
        good_tx(**{"from": RELAYER, "to": WRAPPER, "input": "0xdeadbeef"}),
        ENVELOPE,
        rpc_chain_id=CHAIN_ID,
        txn_hash=HASH,
    )
    assert not r.ok
    names = {c.name for c in r.failed}
    assert "tx.from == envelope.from" in names
    assert "tx.to == envelope.to" in names
    assert "tx.input == envelope.data" in names
