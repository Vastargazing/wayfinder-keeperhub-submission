"""Receipt / transaction fixtures.

Synthetic fixtures. Supply/Withdraw layouts follow saved logs in
``evidence/11-event-signatures.txt``; ERC-20 events use canonical ABI layouts.
The wrapper transaction is a model, not an observed Turnkey transaction.
"""

from __future__ import annotations

from typing import Any

from eth_utils import keccak

CHAIN_ID = 84532
POOL = "0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27"
USDC = "0xba50Cd2A20f6DA35D788639E581bca8d0B5d4D5f"
AUSDC = "0x10F1A9D11CDf50041f3f8cB7191CBE2f31750ACC"
FAUCET = "0xD9145b5F45Ad4519c7ACcD6E0A4A82e83bB8A6Dc"
WALLET = "0x7BEa5c59c12aebF9343087EF1513f651678291F5"
RELAYER = "0x1111111111111111111111111111111111111111"
WRAPPER = "0x2222222222222222222222222222222222222222"
STRANGER = "0x3333333333333333333333333333333333333333"
HASH = "0x" + "ab" * 32

AMOUNT = 1_000_000  # 1.000000 USDC


def t0(sig: str) -> str:
    return "0x" + keccak(text=sig).hex()


SUPPLY_T0 = t0("Supply(address,address,address,uint256,uint16)")
WITHDRAW_T0 = t0("Withdraw(address,address,address,uint256)")
TRANSFER_T0 = t0("Transfer(address,address,uint256)")
APPROVAL_T0 = t0("Approval(address,address,uint256)")


def word_addr(a: str) -> str:
    return "0x" + a.lower().replace("0x", "").rjust(64, "0")


def word_uint(n: int) -> str:
    return "0x" + f"{n:064x}"


def raw_uint(n: int) -> str:
    return f"{n:064x}"


def raw_addr(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def supply_log(
    *,
    emitter: str = POOL,
    reserve: str = USDC,
    user: str = WALLET,
    on_behalf_of: str = WALLET,
    amount: int = AMOUNT,
    referral: int = 0,
    index: int = 0,
) -> dict[str, Any]:
    return {
        "address": emitter,
        "logIndex": hex(index),
        "topics": [SUPPLY_T0, word_addr(reserve), word_addr(on_behalf_of), word_uint(referral)],
        "data": "0x" + raw_addr(user) + raw_uint(amount),
    }


def withdraw_log(
    *,
    emitter: str = POOL,
    reserve: str = USDC,
    user: str = WALLET,
    to: str = WALLET,
    amount: int = AMOUNT,
    index: int = 0,
) -> dict[str, Any]:
    return {
        "address": emitter,
        "logIndex": hex(index),
        "topics": [WITHDRAW_T0, word_addr(reserve), word_addr(user), word_addr(to)],
        "data": word_uint(amount),
    }


def transfer_log(*, emitter: str, sender: str, to: str, value: int, index: int = 0) -> dict[str, Any]:
    return {
        "address": emitter,
        "logIndex": hex(index),
        "topics": [TRANSFER_T0, word_addr(sender), word_addr(to)],
        "data": word_uint(value),
    }


def approval_log(*, emitter: str = USDC, owner: str = WALLET, spender: str = POOL, value: int = AMOUNT, index: int = 0) -> dict[str, Any]:
    return {
        "address": emitter,
        "logIndex": hex(index),
        "topics": [APPROVAL_T0, word_addr(owner), word_addr(spender)],
        "data": word_uint(value),
    }


ZERO = "0x0000000000000000000000000000000000000000"


def sponsored_supply_receipt(**overrides: Any) -> dict[str, Any]:
    """The shape a Turnkey Gas Station supply produces.

    Top level belongs to the relayer/wrapper; the supply is an internal call, so
    only the logs carry it.
    """
    logs = overrides.pop(
        "logs",
        [
            transfer_log(emitter=USDC, sender=WALLET, to=AUSDC, value=AMOUNT, index=0),
            transfer_log(emitter=AUSDC, sender=ZERO, to=WALLET, value=AMOUNT, index=1),
            supply_log(index=2),
        ],
    )
    receipt = {
        "transactionHash": HASH,
        "status": "0x1",
        "from": RELAYER,
        "to": WRAPPER,
        "blockNumber": "0x2c4b3c6",
        "logs": logs,
    }
    receipt.update(overrides)
    return receipt


def sponsored_tx(**overrides: Any) -> dict[str, Any]:
    tx = {
        "hash": HASH,
        "from": RELAYER,
        "to": WRAPPER,
        "input": "0xdeadbeef",
        "value": "0x0",
        "chainId": hex(CHAIN_ID),
    }
    tx.update(overrides)
    return tx


def executed_call_supply(**overrides: Any) -> dict[str, Any]:
    ec = {
        "contractAddress": POOL,
        "functionName": "supply",
        "functionSignature": "supply(address,uint256,address,uint16)",
        "args": {
            "asset": USDC,
            "amount": str(AMOUNT),
            "onBehalfOf": WALLET,
            "referralCode": "0",
        },
        "sponsored": True,
        "topLevelTo": WRAPPER.lower(),
        "reverted": False,
    }
    ec.update(overrides)
    return ec
