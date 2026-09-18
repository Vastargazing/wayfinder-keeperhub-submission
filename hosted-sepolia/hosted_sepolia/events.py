"""Event-log decoding for the calls this path makes.

Supply and Withdraw layouts have saved Base Sepolia log examples in
``evidence/11-event-signatures.txt``. Transfer and Approval use canonical ERC-20
ABI shapes; equivalent real-log witnesses were not saved. The decoded shapes:

``Supply``   topics = [topic0, reserve, onBehalfOf, referralCode], data = (user, amount)
``Withdraw`` topics = [topic0, reserve, user, to],                 data = (amount,)
``Transfer`` topics = [topic0, from, to],                          data = (value,)
``Approval`` topics = [topic0, owner, spender],                    data = (value,)

Note the asymmetry that a specification alone makes easy to get wrong: in
``Supply`` the *user* is a data word and ``onBehalfOf`` is indexed; in
``Withdraw`` both are indexed. Getting this backwards would silently compare the
wrong address, which is exactly the failure this module exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_utils import keccak, to_checksum_address

# ---------------------------------------------------------------------------
# Canonical signatures. `topic0` is keccak256 of the canonical text form.
# ---------------------------------------------------------------------------

SUPPLY_SIG = "Supply(address,address,address,uint256,uint16)"
WITHDRAW_SIG = "Withdraw(address,address,address,uint256)"
TRANSFER_SIG = "Transfer(address,address,uint256)"
APPROVAL_SIG = "Approval(address,address,uint256)"


def topic0(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


SUPPLY_TOPIC0 = topic0(SUPPLY_SIG)
WITHDRAW_TOPIC0 = topic0(WITHDRAW_SIG)
TRANSFER_TOPIC0 = topic0(TRANSFER_SIG)
APPROVAL_TOPIC0 = topic0(APPROVAL_SIG)


class MalformedLog(ValueError):
    """A log whose shape does not match the event it claims to be."""


def _word(hexstr: str) -> bytes:
    raw = bytes.fromhex(hexstr[2:] if hexstr.startswith("0x") else hexstr)
    if len(raw) != 32:
        raise MalformedLog(f"expected a 32-byte word, got {len(raw)} bytes")
    return raw


def _addr_from_word(raw: bytes) -> str:
    """Decode an address word, refusing dirty padding.

    Twelve zero bytes of padding are mandatory. A word with anything else in
    them is not an address this log can be read as, and accepting it would let a
    crafted log present a different address than the one that is checked.
    """
    if raw[:12] != b"\x00" * 12:
        raise MalformedLog("address word has non-zero padding bytes")
    return to_checksum_address("0x" + raw[12:].hex())


def _uint_from_word(raw: bytes) -> int:
    return int.from_bytes(raw, "big")


def _data_words(data: Any) -> list[bytes]:
    if not isinstance(data, str) or not data.startswith("0x"):
        raise MalformedLog("log data is not a 0x-prefixed string")
    raw = bytes.fromhex(data[2:])
    if len(raw) % 32:
        raise MalformedLog(f"log data is not a whole number of words ({len(raw)} bytes)")
    return [raw[i : i + 32] for i in range(0, len(raw), 32)]


def _topics(log: dict[str, Any]) -> list[str]:
    topics = log.get("topics")
    if not isinstance(topics, list) or not topics:
        raise MalformedLog("log has no topics")
    for t in topics:
        if not isinstance(t, str) or len(t) != 66 or not t.startswith("0x"):
            raise MalformedLog("log topic is not a 32-byte hex word")
    return topics


@dataclass(frozen=True)
class DecodedEvent:
    """One decoded log, with the emitter kept alongside the fields.

    ``emitter`` is deliberately part of the decoded value: a ``Supply`` event is
    only meaningful if the contract that emitted it is the pool we targeted.
    Anyone can deploy a contract that emits an identically-shaped log.
    """

    name: str
    emitter: str
    log_index: int | None
    fields: dict[str, Any]


_DECODERS: dict[str, tuple[str, int, int]] = {
    # topic0 -> (name, expected topic count incl. topic0, expected data words)
    SUPPLY_TOPIC0: ("Supply", 4, 2),
    WITHDRAW_TOPIC0: ("Withdraw", 4, 1),
    TRANSFER_TOPIC0: ("Transfer", 3, 1),
    APPROVAL_TOPIC0: ("Approval", 3, 1),
}


def _decode_log(log: dict[str, Any]) -> DecodedEvent | None:
    """Decode one receipt log, or return ``None`` if it is not one we know.

    Raises :class:`MalformedLog` when a log *claims* a known ``topic0`` but does
    not have that event's shape. That is treated as an error rather than an
    unknown log on purpose. The caller must first select relevant emitters:
    topic0 alone does not distinguish ERC-20 from ERC-721 indexed fields.
    """
    if log.get("topics") == []:
        # EVM LOG0/anonymous events need not have ABI word-sized data.
        data = log.get("data")
        if not isinstance(data, str) or not data.startswith("0x"):
            raise MalformedLog("LOG0 data must be hex")
        bytes.fromhex(data[2:])
        to_checksum_address(log["address"])
        return None
    topics = _topics(log)
    entry = _DECODERS.get(topics[0].lower())
    if entry is None:
        return None
    name, n_topics, n_words = entry
    if len(topics) != n_topics:
        raise MalformedLog(f"{name}: expected {n_topics} topics, got {len(topics)}")
    words = _data_words(log.get("data", "0x"))
    if len(words) != n_words:
        raise MalformedLog(f"{name}: expected {n_words} data words, got {len(words)}")

    emitter_raw = log.get("address")
    if not isinstance(emitter_raw, str):
        raise MalformedLog("log has no emitter address")
    emitter = to_checksum_address(emitter_raw)

    raw_index = log.get("logIndex")
    if isinstance(raw_index, str):
        log_index: int | None = int(raw_index, 16)
    elif isinstance(raw_index, int):
        log_index = raw_index
    else:
        log_index = None

    if name == "Supply":
        fields = {
            "reserve": _addr_from_word(_word(topics[1])),
            "onBehalfOf": _addr_from_word(_word(topics[2])),
            "referralCode": _uint_from_word(_word(topics[3])),
            "user": _addr_from_word(words[0]),
            "amount": _uint_from_word(words[1]),
        }
    elif name == "Withdraw":
        fields = {
            "reserve": _addr_from_word(_word(topics[1])),
            "user": _addr_from_word(_word(topics[2])),
            "to": _addr_from_word(_word(topics[3])),
            "amount": _uint_from_word(words[0]),
        }
    elif name == "Transfer":
        fields = {
            "from": _addr_from_word(_word(topics[1])),
            "to": _addr_from_word(_word(topics[2])),
            "value": _uint_from_word(words[0]),
        }
    else:  # Approval
        fields = {
            "owner": _addr_from_word(_word(topics[1])),
            "spender": _addr_from_word(_word(topics[2])),
            "value": _uint_from_word(words[0]),
        }

    return DecodedEvent(name=name, emitter=emitter, log_index=log_index, fields=fields)


def decode_log(log: dict[str, Any]) -> DecodedEvent | None:
    try:
        return _decode_log(log)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise MalformedLog(str(exc)) from exc


def decode_receipt_logs(
    receipt: dict[str, Any], *, relevant_emitters: set[str] | None = None
) -> list[DecodedEvent]:
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        raise MalformedLog("receipt has no logs array")
    emitters = ({to_checksum_address(address) for address in relevant_emitters}
                if relevant_emitters is not None else None)
    out: list[DecodedEvent] = []
    for log in logs:
        if not isinstance(log, dict):
            raise MalformedLog("receipt log is not an object")
        if emitters is not None:
            try:
                emitter = to_checksum_address(log["address"])
            except (ValueError, TypeError, KeyError) as exc:
                raise MalformedLog("Invalid receipt log emitter") from exc
            if emitter not in emitters:
                continue
        decoded = decode_log(log)
        if decoded is not None:
            out.append(decoded)
    return out
