"""Round-trip a Wayfinder envelope's raw calldata into KeeperHub's
ABI + functionName + args triple, and refuse if the bytes do not come back.

KeeperHub's ``web3/write-contract`` node does not take raw calldata: it takes
an ABI, a function name and a JSON array of arguments, and re-encodes them with
ethers (`lib/web3/chain-adapter/evm.ts:210-213`). The Wayfinder seam hands us
``{chainId, from, to, data, value}`` with ``data`` already encoded. So the
bridge is a decode followed by a re-encode, and the only honest way to run it is
to prove the re-encode reproduces the original bytes *before* anything is
submitted.

`research/keeperhub/BRAP-CALLDATA.md` establishes that KeeperHub's arg pipeline
(`reshapeArgsForAbi` / `coerceArgsForAbi` / `validateArgsForAbi`) round-trips
19 of 22 real router entrypoints byte-for-byte, including tuple arrays with
`bytes` and `bool` members, and names the three families that cannot round-trip
at all (1inch's 4-byte suffix, Odos `swapCompact()`, 0x AllowanceHolder's
fallback-only ABI). This module is the client-side half of that: it produces the
exact JSON a workflow author would type, feeds it back through a JSON round trip
and an ABI encode, and compares bytes.

There is no "close enough". :class:`CalldataReproductionError` is raised and the
caller must not submit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

ARRAY_SUFFIX = re.compile(r"^(.*)\[(\d*)\]$")


class CalldataReproductionError(RuntimeError):
    """The re-encoded calldata is not byte-identical to the envelope's.

    Raised before any submission. Carries the diagnostic bytes so the refusal
    is auditable: an operator can see exactly where the two encodings diverge.
    """

    def __init__(
        self,
        message: str,
        *,
        original: str | None = None,
        reencoded: str | None = None,
        first_diff: int | None = None,
    ) -> None:
        self.original = original
        self.reencoded = reencoded
        self.first_diff = first_diff
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "original": self.original,
            "reencoded": self.reencoded,
            "original_len_bytes": len(self.original or "0x") // 2 - 1,
            "reencoded_len_bytes": (
                len(self.reencoded) // 2 - 1 if self.reencoded is not None else None
            ),
            "first_differing_byte": self.first_diff,
        }


@dataclass(frozen=True)
class DecodedCall:
    """What KeeperHub's write-contract node needs, plus the proof it is right."""

    function_name: str
    signature: str
    selector: str
    abi: list[dict[str, Any]]
    function_abi: dict[str, Any]
    args: list[Any]  # JSON-ready: the exact value that goes into functionArgs
    reencoded: str
    original: str
    verified: bool = field(default=True)

    def function_args_json(self) -> str:
        """The `functionArgs` config string, exactly as an author would type it."""
        return json.dumps(self.args)

    def abi_json(self) -> str:
        return json.dumps(self.abi)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "functionName": self.function_name,
            "signature": self.signature,
            "selector": self.selector,
            "args": self.args,
            "original_calldata": self.original,
            "reencoded_calldata": self.reencoded,
            "byte_equal": self.original.lower() == self.reencoded.lower(),
            "calldata_len_bytes": len(self.original) // 2 - 1,
        }


# --------------------------------------------------------------------------
# ABI type plumbing
# --------------------------------------------------------------------------


def canonical_type(entry: dict[str, Any]) -> str:
    """Expand a tuple ABI input into its canonical `(a,b)` signature form."""
    typ = entry.get("type", "")
    if typ.startswith("tuple"):
        inner = ",".join(canonical_type(c) for c in entry.get("components", []))
        return f"({inner}){typ[len('tuple'):]}"
    return typ


def function_signature(fn: dict[str, Any]) -> str:
    return f"{fn['name']}({','.join(canonical_type(i) for i in fn.get('inputs', []))})"


def selector_of(fn: dict[str, Any]) -> str:
    return "0x" + keccak(text=function_signature(fn))[:4].hex()


def _to_json_value(value: Any, typ: str, components: list[dict] | None) -> Any:
    """Serialise one decoded ABI value into the JSON shape KeeperHub accepts.

    Integers become decimal strings (never JSON numbers: BRAP-CALLDATA.md §5.3
    observed a raw JSON number above 2^53 being parsed to a float and rejected
    by ethers as an overflow). Tuples become name-keyed objects, which is what
    the config UI emits and what `validateArgsForAbi` requires.
    """
    m = ARRAY_SUFFIX.match(typ)
    if m:
        base = m.group(1)
        return [_to_json_value(v, base, components) for v in value]
    if typ.startswith("tuple"):
        comps = components or []
        if len(comps) != len(value):
            raise CalldataReproductionError(
                f"tuple arity mismatch: abi has {len(comps)} components, decoded {len(value)}"
            )
        out: dict[str, Any] = {}
        for comp, item in zip(comps, value, strict=True):
            name = comp.get("name")
            if not name:
                raise CalldataReproductionError(
                    "tuple component has no name; KeeperHub's validateArgsForAbi "
                    "requires a name-keyed object for tuples"
                )
            out[name] = _to_json_value(item, comp["type"], comp.get("components"))
        return out
    if typ == "address":
        return to_checksum_address(value)
    if typ.startswith(("uint", "int")):
        return str(value)
    if typ == "bool":
        return bool(value)
    if typ == "string":
        return value
    if typ.startswith("bytes"):
        return "0x" + bytes(value).hex()
    raise CalldataReproductionError(f"unsupported ABI type for JSON round trip: {typ}")


def _from_json_value(value: Any, typ: str, components: list[dict] | None) -> Any:
    """Inverse of :func:`_to_json_value` — what an encoder must see."""
    m = ARRAY_SUFFIX.match(typ)
    if m:
        base = m.group(1)
        return [_from_json_value(v, base, components) for v in value]
    if typ.startswith("tuple"):
        comps = components or []
        return tuple(
            _from_json_value(value[c["name"]], c["type"], c.get("components"))
            for c in comps
        )
    if typ == "address":
        return to_checksum_address(value)
    if typ.startswith(("uint", "int")):
        return int(value, 0) if isinstance(value, str) else int(value)
    if typ == "bool":
        return value if isinstance(value, bool) else str(value).lower() == "true"
    if typ == "string":
        return value
    if typ.startswith("bytes"):
        return bytes.fromhex(value[2:] if value.startswith("0x") else value)
    raise CalldataReproductionError(f"unsupported ABI type: {typ}")


def find_function_by_selector(
    abi: list[dict[str, Any]], selector: str
) -> dict[str, Any] | None:
    want = selector.lower()
    for entry in abi:
        if entry.get("type") != "function":
            continue
        if selector_of(entry).lower() == want:
            return entry
    return None


# --------------------------------------------------------------------------
# The decisive test
# --------------------------------------------------------------------------


def decode_and_verify(data: str, abi: list[dict[str, Any]]) -> DecodedCall:
    """Decode `data` against `abi`, re-encode, and refuse unless bytes match.

    The re-encode deliberately runs from the *JSON round-tripped* arguments —
    the same bytes that will travel over HTTP into KeeperHub's `functionArgs`
    field — not from the freshly decoded Python objects. A round trip that only
    proves "the decoder and the encoder agree" would miss exactly the
    serialisation faults BRAP-CALLDATA.md §5.3 catalogues.
    """
    if not isinstance(data, str) or not data.startswith("0x"):
        raise CalldataReproductionError(f"calldata must be a 0x string, got {data!r}")
    raw = bytes.fromhex(data[2:])
    if len(raw) < 4:
        raise CalldataReproductionError(
            f"calldata is {len(raw)} bytes; a function call needs at least a 4-byte selector"
        )
    selector = "0x" + raw[:4].hex()
    tail = raw[4:]

    fn = find_function_by_selector(abi, selector)
    if fn is None:
        raise CalldataReproductionError(
            f"no function in the supplied ABI has selector {selector}. "
            "KeeperHub's write-contract node can only submit calls it can name "
            "(findAbiFunction filters type=='function'), so a fallback-only or "
            "unknown entrypoint cannot be reproduced."
        )

    types = [canonical_type(i) for i in fn.get("inputs", [])]
    try:
        decoded = abi_decode(types, tail, strict=True)
    except Exception as exc:  # noqa: BLE001
        raise CalldataReproductionError(
            f"strict ABI decode of {selector} failed: {exc}. The calldata is not "
            "canonical ABI encoding, so no ABI-driven encoder can reproduce it."
        ) from exc

    args_json = [
        _to_json_value(v, i["type"], i.get("components"))
        for v, i in zip(decoded, fn.get("inputs", []), strict=True)
    ]

    # Round-trip through JSON exactly as the HTTP payload will.
    args_after_json = json.loads(json.dumps(args_json))
    values = [
        _from_json_value(v, i["type"], i.get("components"))
        for v, i in zip(args_after_json, fn.get("inputs", []), strict=True)
    ]
    reencoded = "0x" + raw[:4].hex() + abi_encode(types, values).hex()

    if reencoded.lower() != data.lower():
        first = _first_diff(data, reencoded)
        raise CalldataReproductionError(
            f"re-encoded calldata for {function_signature(fn)} is not byte-identical "
            f"to the envelope ({len(raw)} bytes original, "
            f"{len(reencoded) // 2 - 1} bytes re-encoded, first difference at byte "
            f"{first}). Refusing to submit: KeeperHub would broadcast bytes we did "
            "not verify.",
            original=data,
            reencoded=reencoded,
            first_diff=first,
        )

    return DecodedCall(
        function_name=fn["name"],
        signature=function_signature(fn),
        selector=selector,
        abi=abi,
        function_abi=fn,
        args=args_after_json,
        reencoded=reencoded,
        original=data,
    )


def _first_diff(a: str, b: str) -> int | None:
    ra = bytes.fromhex(a[2:])
    rb = bytes.fromhex(b[2:])
    for i in range(min(len(ra), len(rb))):
        if ra[i] != rb[i]:
            return i
    if len(ra) != len(rb):
        return min(len(ra), len(rb))
    return None
