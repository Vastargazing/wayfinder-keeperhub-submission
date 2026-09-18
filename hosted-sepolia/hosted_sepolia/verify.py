"""Two verification profiles, selected by the execution mode KeeperHub reports.

Why two profiles at all
-----------------------

``integration``'s executor validates an attributed transaction by
comparing the *top level* of ``eth_getTransactionByHash`` against the operation's
envelope: chain, ``from``, ``to``, ``input``, ``value``
(``keeperhub_executor/executor.py``, ``_with_execution``). That is correct for a
direct send and it is not weakened anywhere in this module.

A gas-sponsored send does not have that shape. SOURCE, pinned KeeperHub
``stand/local-fork`` @ ``57b7be4``:

- ``lib/web3/turnkey-sponsored-tx.ts:16-24`` — sponsorship is Turnkey's Gas
  Station: one ``ethSendTransaction`` with ``sponsor: true``. Not ERC-4337.
- ``tests/unit/verify-receipt.test.ts:558-562`` — "Turnkey's Gas Station
  delegates the org wallet via EIP-7702, and a relayer EOA calls an executor
  contract which invokes the wallet."
- ``docs/wallet-management/onchain-appearance.md:29-42`` — top-level ``from`` is
  the relayer, top-level ``to`` is "a contract you do not recognise",
  top-level ``value`` is 0, and "your own action ... is an internal call".

So on the sponsored path the top-level ``from``/``to``/``input``/``value`` all
belong to the wrapper. Running the direct comparison against them fails. The
answer is not to drop fields from the comparison; it is to compare a different
thing — the effective call — and to corroborate it from a source that is not
KeeperHub.

The independence rule
---------------------

``moonwell``'s gate requires two checks that cannot substitute for each
other: **ownership** (KeeperHub's own record binds this hash to this operation
id) and **content** (the transaction is the call the SDK authorized, compared
against the envelope the SDK journalled *before* the executor was called).

KeeperHub also reports ``executedCall``, a reconstruction of the effective call
(SOURCE ``lib/web3/trace-decode.ts:281-307``, built from
``debug_traceTransaction`` with ``callTracer``). It is tempting to use it as the
content check on the sponsored path. That would be wrong: ownership already
comes from KeeperHub. Using ``executedCall`` for content would make both
checks depend on KeeperHub's account of the transaction.

Therefore, on the sponsored path:

- **ownership** stays KeeperHub's (unchanged);
- **content** is the SDK's journalled envelope, decoded, checked against the
  **receipt's event logs**, which come from any RPC and are not derived from
  KeeperHub at all;
- ``executedCall``, when present, is KeeperHub corroboration that must agree.
  It is never a substitute for the log check, and its absence never satisfies
  the log check.

What an event log proves, and what it does not
----------------------------------------------

A log proves that **an effect occurred inside that transaction**: the named
contract executed the code path that emits that event, with those values, in
that transaction. Bound on emitter, reserve, user, ``onBehalfOf`` and amount,
that establishes the expected effect, subject to pinned contract semantics.

It does **not** prove that the transaction contained **only** our call. A
wrapper is free to batch. Nothing in a receipt's logs rules out other calls in
the same transaction, including other calls that touch the same wallet. So a
``landed`` answer in sponsored mode means "the operation's effect is on chain in
this transaction", not "this transaction is exactly and only this operation".
The consequences are spelled out in ``exclusivity_caveat()`` and are carried in
every :class:`VerificationResult` this module produces, so a caller cannot
quietly inherit the direct profile's stronger meaning.

One more limit worth naming: the outer receipt's ``status`` is not by itself
proof that the inner call succeeded. KeeperHub says so about its own code
(``tests/unit/verify-receipt.test.ts:565-572``: safe "only while the executor
propagates an inner failure to the outer status ... treat it as a statement of
what we rely on Turnkey for, and not as proof that sponsorship is safe by
construction"). The event-log check is what closes that gap here: the pool emits
``Supply`` only if the supply actually executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any

from eth_utils import to_checksum_address

from hosted_sepolia.events import DecodedEvent, MalformedLog, decode_receipt_logs

MAX_UINT256 = 2**256 - 1


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One named comparison, with both sides recorded."""

    name: str
    passed: bool
    expected: Any = None
    observed: Any = None
    source: str = ""
    detail: str = ""


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    profile: str
    checks: tuple[Check, ...]
    blockers: tuple[str, ...] = ()
    evidence_origins: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.passed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "profile": self.profile,
            "evidence_origins": list(self.evidence_origins),
            "blockers": list(self.blockers),
            "caveats": list(self.caveats),
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "expected": _jsonable(c.expected),
                    "observed": _jsonable(c.observed),
                    "source": c.source,
                    "detail": c.detail,
                }
                for c in self.checks
            ],
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value) if value > 2**53 else value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def exclusivity_caveat() -> str:
    return (
        "Event logs prove an effect occurred inside this transaction; they do "
        "not prove the transaction contained only this call. A sponsored "
        "wrapper may batch. 'landed' here means the operation's effect is on "
        "chain in this transaction, not that this transaction is exactly and "
        "only this operation."
    )


def outer_status_caveat() -> str:
    return (
        "The outer receipt's status is the relayer transaction's status. That "
        "it reflects an inner failure is a property of Turnkey's executor that "
        "KeeperHub relies on and does not prove "
        "(tests/unit/verify-receipt.test.ts:565-572). The event-log check, not "
        "the status, is what establishes that the inner call executed."
    )


# ---------------------------------------------------------------------------
# Mode classification — reported, never guessed
# ---------------------------------------------------------------------------


class ExecutionMode(Enum):
    DIRECT = "direct"
    SPONSORED = "sponsored"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModeFinding:
    mode: ExecutionMode
    sources: tuple[str, ...]
    detail: str


class ModePayload(dict):
    """HTTP client attaches provenance outside the untrusted JSON payload."""

    def __init__(self, payload: dict[str, Any], provenance: str):
        super().__init__(payload)
        self.mode_provenance = provenance


def classify_execution_mode(
    node_output: dict[str, Any] | None,
    node_output_raw: dict[str, Any] | None = None,
    *,
    status_output: dict[str, Any] | None = None,
) -> ModeFinding:
    """Collect every flag; a conflict never selects a profile.

    Plain dictionaries mean persisted node output. ModePayload from the status
    client is synthesized by Boolean(output?.sponsored): false is no evidence.
    Trace flags describe call shape, not the fee payer. Alone they leave UNKNOWN;
    when present they must agree with all explicit flags. This is classification
    of reported mode, not independent proof that Turnkey paid gas.
    """
    explicit: list[tuple[bool, str]] = []
    trace: list[tuple[bool, str]] = []
    notes: list[str] = []
    payloads = [
        ("output", node_output, "stored"),
        ("outputRaw", node_output_raw, "stored"),
        ("status", status_output, "status_boolean"),
    ]
    for label, payload, default_origin in payloads:
        if not isinstance(payload, dict):
            continue
        origin = getattr(payload, "mode_provenance", default_origin)
        flag = payload.get("sponsored")
        # The real status route also returns output as `result`; preserve that
        # persisted source instead of hiding it behind the synthesized boolean.
        if origin == "status_boolean" and isinstance(payload.get("result"), dict):
            payloads.append((label + ".result", ModePayload(payload["result"], "stored"), "stored"))
            stored_flag = payload["result"].get("sponsored")
            if isinstance(stored_flag, bool) and isinstance(flag, bool) and stored_flag != flag:
                return ModeFinding(ExecutionMode.UNKNOWN, (label + ".sponsored", label + ".result.sponsored"),
                                   "Conflict between status synthesis and its persisted result.")
        if isinstance(flag, bool):
            src = f"{label}.sponsored"
            if origin == "status_boolean" and flag is False:
                notes.append(f"{src}=false synthesized; direct or absent")
            elif origin in {"stored", "status_boolean"}:
                explicit.append((flag, src))
            else:
                notes.append(f"{src}: unknown provenance")
        ec = payload.get("executedCall")
        if isinstance(ec, dict) and isinstance(ec.get("sponsored"), bool):
            trace.append((ec["sponsored"], f"{label}.executedCall.sponsored"))
    flags = explicit + trace
    sources = tuple(src for _, src in flags) + tuple(notes)
    if len({flag for flag, _ in flags}) > 1:
        return ModeFinding(ExecutionMode.UNKNOWN, sources,
                           "Conflicting mode sources; conflict requires reconciliation.")
    if explicit:
        return ModeFinding(ExecutionMode.SPONSORED if explicit[0][0] else ExecutionMode.DIRECT,
                           sources, "Consistent explicitly reported mode; trace is only call-shape corroboration.")
    return ModeFinding(ExecutionMode.UNKNOWN, sources,
                       "No explicit execution mode: absence cannot be read as direct. "
                       "Synthesized false and trace shape do not establish the fee payer.")


# ---------------------------------------------------------------------------
# Profile 1 — direct. Unchanged semantics.
# ---------------------------------------------------------------------------


def verify_direct_envelope(
    tx: dict[str, Any] | None,
    envelope: dict[str, Any],
    *,
    rpc_chain_id: int,
    txn_hash: str,
) -> VerificationResult:
    """The existing top-level comparison, reproduced exactly.

    chain, sender, recipient, calldata and value, all against the top level of
    ``eth_getTransactionByHash``. Nothing here is relaxed for sponsorship; the
    sponsored path uses a different function.
    """
    checks: list[Check] = []
    want_chain = int(envelope["chainId"])
    checks.append(
        Check(
            "rpc_chain_id",
            rpc_chain_id == want_chain,
            want_chain,
            rpc_chain_id,
            "eth_chainId",
        )
    )
    if not tx:
        checks.append(Check("transaction_present", False, txn_hash, None, "eth_getTransactionByHash"))
        return VerificationResult(
            False,
            "direct",
            tuple(checks),
            ("attributed transaction not found on chain",),
            ("chain: eth_getTransactionByHash"),
        )
    checks.append(Check("transaction_present", True, txn_hash, txn_hash, "eth_getTransactionByHash"))

    want_data = str(envelope.get("data") or "0x").lower()
    pairs = [
        ("tx.hash", txn_hash.lower(), str(tx.get("hash", "")).lower()),
        ("tx.from == envelope.from", str(envelope["from"]).lower(), str(tx.get("from", "")).lower()),
        ("tx.to == envelope.to", str(envelope["to"]).lower(), str(tx.get("to") or "").lower()),
        ("tx.input == envelope.data", want_data, str(tx.get("input", "")).lower()),
    ]
    for name, want, got in pairs:
        checks.append(Check(name, want == got, want, got, "eth_getTransactionByHash"))

    want_value = int(envelope.get("value") or 0)
    got_value = _hex_or_int(tx.get("value", 0))
    checks.append(Check("tx.value == envelope.value", want_value == got_value, want_value, got_value, "eth_getTransactionByHash"))

    # A transaction may carry its own chainId; when it does it must agree too.
    tx_chain = tx.get("chainId")
    if tx_chain is not None:
        got_chain = _hex_or_int(tx_chain)
        checks.append(Check("tx.chainId", got_chain == want_chain, want_chain, got_chain, "eth_getTransactionByHash"))

    ok = all(c.passed for c in checks)
    return VerificationResult(
        ok,
        "direct",
        tuple(checks),
        () if ok else tuple(f"{c.name}: expected {c.expected!r}, observed {c.observed!r}" for c in checks if not c.passed),
        ("chain: eth_getTransactionByHash top-level fields"),
    )


def _hex_or_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    raise TypeError(f"not a number: {value!r}")


# ---------------------------------------------------------------------------
# Profile 2 — sponsored.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectExpectation:
    """What the SDK authorized, expressed as effects the chain must show.

    Built from the SDK's own journalled envelope — never from KeeperHub's
    ``executedCall`` and never from KeeperHub's recorded input.
    """

    kind: str  # "supply" | "withdraw" | "approve" | "erc20_mint"
    chain_id: int  # required, and never defaulted: a wrong chain is a wrong proof
    target: str  # the contract the call was addressed to
    wallet: str  # our Turnkey wallet: msg.sender and position owner
    asset: str | None = None
    amount: int | None = None
    spender: str | None = None
    recipient: str | None = None
    a_token: str | None = None
    referral_code: int | None = None
    exact_amount: bool = True

    def normalised(self) -> EffectExpectation:
        conv = to_checksum_address
        return EffectExpectation(
            kind=self.kind,
            chain_id=int(self.chain_id),
            target=conv(self.target),
            wallet=conv(self.wallet),
            asset=conv(self.asset) if self.asset else None,
            amount=self.amount,
            spender=conv(self.spender) if self.spender else None,
            recipient=conv(self.recipient) if self.recipient else None,
            a_token=conv(self.a_token) if self.a_token else None,
            referral_code=self.referral_code,
            exact_amount=self.exact_amount,
        )


def decode_event_logs(
    receipt: dict[str, Any], *, relevant_emitters: set[str] | None = None
) -> list[DecodedEvent]:
    return decode_receipt_logs(receipt, relevant_emitters=relevant_emitters)


def _match_supply(events: list[DecodedEvent], want: EffectExpectation) -> list[Check]:
    checks: list[Check] = []
    candidates = [
        e
        for e in events
        if e.name == "Supply" and e.emitter.lower() == want.target.lower()
    ]
    checks.append(
        Check(
            "log.Supply emitted by the target pool",
            len(candidates) == 1,
            f"exactly 1 Supply from {want.target}",
            f"{len(candidates)} found",
            "receipt.logs",
            "Zero means the supply did not execute here; more than one means "
            "this transaction supplied more than once and the effect cannot be "
            "attributed to this operation alone.",
        )
    )
    if len(candidates) != 1:
        return checks
    f = candidates[0].fields
    checks.append(Check("Supply.reserve == asset", f["reserve"].lower() == (want.asset or "").lower(), want.asset, f["reserve"], "receipt.logs"))
    checks.append(
        Check(
            "Supply.user == our wallet",
            f["user"].lower() == want.wallet.lower(),
            want.wallet,
            f["user"],
            "receipt.logs",
            "Aave sets `user` to msg.sender and pulls the underlying from it. "
            "Under EIP-7702 the delegated code runs in our EOA's context, so "
            "this must be our wallet even though the relayer sent the outer tx. "
            "A different value means someone else's tokens funded the position.",
        )
    )
    checks.append(
        Check(
            "Supply.onBehalfOf == our wallet",
            f["onBehalfOf"].lower() == want.wallet.lower(),
            want.wallet,
            f["onBehalfOf"],
            "receipt.logs",
            "This is the ownership invariant: the aTokens are minted to "
            "onBehalfOf. The top-level sender is the relayer; onBehalfOf is "
            "what proves the position is ours.",
        )
    )
    checks.append(_amount_check("Supply.amount", f["amount"], want))
    if want.referral_code is not None:
        checks.append(Check("Supply.referralCode", f["referralCode"] == want.referral_code, want.referral_code, f["referralCode"], "receipt.logs"))

    # Corroborating ERC-20 movement: the underlying leaves our wallet for the aToken.
    if want.asset:
        transfers = [
            e
            for e in events
            if e.name == "Transfer"
            and e.emitter.lower() == want.asset.lower()
            and e.fields["from"].lower() == want.wallet.lower()
            and (want.amount is None or e.fields["value"] == want.amount)
        ]
        checks.append(
            Check(
                "underlying Transfer(wallet -> aToken, amount)",
                len(transfers) == 1,
                f"1 Transfer of {want.amount} from {want.wallet} on {want.asset}",
                f"{len(transfers)} found",
                "receipt.logs",
                "Independent of the Supply event: the token contract, not the "
                "pool, says the tokens left our wallet.",
            )
        )
        if len(transfers) == 1 and want.a_token:
            got_to = transfers[0].fields["to"]
            checks.append(Check("underlying Transfer.to == aToken", got_to.lower() == want.a_token.lower(), want.a_token, got_to, "receipt.logs"))

    # aToken mint. Deliberately NOT an equality check: Aave emits
    # Transfer(0, onBehalfOf, amount + balanceIncrease), and balanceIncrease is
    # non-zero whenever the position already accrued interest. Requiring
    # equality would make this check fail on every supply after the first.
    if want.a_token and want.amount is not None:
        mints = [
            e
            for e in events
            if e.name == "Transfer"
            and e.emitter.lower() == want.a_token.lower()
            and int(e.fields["from"], 16) == 0
            and e.fields["to"].lower() == want.wallet.lower()
            and e.fields["value"] >= want.amount
        ]
        checks.append(
            Check(
                "aToken mint to our wallet (>= amount)",
                len(mints) >= 1,
                f">= {want.amount} minted to {want.wallet} on {want.a_token}",
                f"{len(mints)} found",
                "receipt.logs",
                "Not an equality: Aave mints amount + accrued balanceIncrease.",
            )
        )
    return checks


def _match_withdraw(events: list[DecodedEvent], want: EffectExpectation) -> list[Check]:
    checks: list[Check] = []
    candidates = [e for e in events if e.name == "Withdraw" and e.emitter.lower() == want.target.lower()]
    checks.append(Check("log.Withdraw emitted by the target pool", len(candidates) == 1, f"exactly 1 Withdraw from {want.target}", f"{len(candidates)} found", "receipt.logs"))
    if len(candidates) != 1:
        return checks
    f = candidates[0].fields
    checks.append(Check("Withdraw.reserve == asset", f["reserve"].lower() == (want.asset or "").lower(), want.asset, f["reserve"], "receipt.logs"))
    checks.append(
        Check(
            "Withdraw.user == our wallet",
            f["user"].lower() == want.wallet.lower(),
            want.wallet,
            f["user"],
            "receipt.logs",
            "The account whose aTokens were burned. This is the ownership "
            "invariant for a withdraw.",
        )
    )
    recipient = want.recipient or want.wallet
    checks.append(Check("Withdraw.to == recipient", f["to"].lower() == recipient.lower(), recipient, f["to"], "receipt.logs"))
    checks.append(_amount_check("Withdraw.amount", f["amount"], want))
    return checks


def _match_approve(events: list[DecodedEvent], want: EffectExpectation) -> list[Check]:
    checks: list[Check] = []
    candidates = [e for e in events if e.name == "Approval" and e.emitter.lower() == want.target.lower()]
    checks.append(Check("log.Approval emitted by the token", len(candidates) == 1, f"exactly 1 Approval from {want.target}", f"{len(candidates)} found", "receipt.logs"))
    if len(candidates) != 1:
        return checks
    f = candidates[0].fields
    checks.append(Check("Approval.owner == our wallet", f["owner"].lower() == want.wallet.lower(), want.wallet, f["owner"], "receipt.logs"))
    checks.append(Check("Approval.spender", f["spender"].lower() == (want.spender or "").lower(), want.spender, f["spender"], "receipt.logs"))
    checks.append(Check("Approval.value", f["value"] == want.amount, want.amount, f["value"], "receipt.logs"))
    return checks


def _match_erc20_mint(events: list[DecodedEvent], want: EffectExpectation) -> list[Check]:
    checks: list[Check] = []
    candidates = [
        e
        for e in events
        if e.name == "Transfer"
        and e.emitter.lower() == (want.asset or "").lower()
        and int(e.fields["from"], 16) == 0
        and e.fields["to"].lower() == want.wallet.lower()
    ]
    checks.append(Check("log.Transfer(0 -> wallet) from the token", len(candidates) == 1, f"exactly 1 mint to {want.wallet} on {want.asset}", f"{len(candidates)} found", "receipt.logs"))
    if len(candidates) != 1:
        return checks
    checks.append(_amount_check("mint amount", candidates[0].fields["value"], want))
    return checks


def _amount_check(name: str, observed: int, want: EffectExpectation) -> Check:
    if want.amount is None:
        return Check(name, False, "authorized uint256 amount", observed, "receipt.logs")
    if want.kind == "withdraw" and want.amount == MAX_UINT256:
        # withdraw(MAX_UINT256) means "all"; the event carries the real amount.
        return Check(
            name,
            observed > 0,
            "> 0 (authorized amount was MAX_UINT256 = 'all')",
            observed,
            "receipt.logs",
            "MAX_UINT256 is a sentinel, not a quantity; the event's amount is "
            "the real one and cannot be compared for equality.",
        )
    return Check(name, observed == want.amount, want.amount, observed, "receipt.logs")


_MATCHERS = {
    "supply": _match_supply,
    "withdraw": _match_withdraw,
    "approve": _match_approve,
    "erc20_mint": _match_erc20_mint,
}


def _verify_sponsored_effect(
    tx: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    expectation: EffectExpectation,
    *,
    rpc_chain_id: int,
    txn_hash: str,
    wallet_address: str,
    executed_call: dict[str, Any] | None = None,
    expected_function: str | None = None,
) -> VerificationResult:
    """Verify a sponsored execution's effective call.

    Requires, all of them:

    1. the receipt is for this hash, on this chain, and succeeded;
    2. the top level is **not** a direct send by our wallet — if it were, the
       direct profile applies and this one would be the weaker answer;
    3. the receipt's event logs show our expected effect: right emitter, right
       reserve, right ``user``/``onBehalfOf``, right amount;
    4. ``executedCall``, **when KeeperHub supplied one**, agrees.

    Point 4 is a corroboration, not the content check. Point 3 is the content
    check, and its source is the chain.
    """
    want = expectation.normalised()
    wallet = to_checksum_address(wallet_address)
    checks: list[Check] = [
        Check("expectation.wallet == wallet_address", want.wallet == wallet, want.wallet, wallet, "caller"),
        Check("authorized amount present", type(want.amount) is int and 0 <= want.amount <= MAX_UINT256,
              "uint256", want.amount, "authorization"),
        Check("amount relaxation authorized", want.exact_amount is True or
              (want.exact_amount is False and want.kind == "withdraw" and want.amount == MAX_UINT256),
              "exact or withdraw(MAX_UINT256)", want.exact_amount, "authorization"),
    ]
    blockers: list[str] = []
    sources = ["chain: receipt event logs"]

    checks.append(
        Check(
            "rpc_chain_id == authorized chain",
            rpc_chain_id == want.chain_id,
            want.chain_id,
            rpc_chain_id,
            "eth_chainId",
            "The RPC we read the receipt from must be the chain the operation "
            "authorized. Reading a matching receipt off the wrong chain proves "
            "nothing about this operation.",
        )
    )

    if receipt is None:
        checks.append(Check("receipt_present", False, txn_hash, None, "eth_getTransactionReceipt"))
        blockers.append("no receipt: the effective call cannot be read")
        return VerificationResult(False, "sponsored", tuple(checks), tuple(blockers), tuple(sources), (outer_status_caveat(),))
    checks.append(Check("receipt_present", True, txn_hash, txn_hash, "eth_getTransactionReceipt"))

    got_hash = str(receipt.get("transactionHash", "")).lower()
    checks.append(Check("receipt.transactionHash == attributed hash", got_hash == txn_hash.lower(), txn_hash.lower(), got_hash, "eth_getTransactionReceipt"))

    status = receipt.get("status")
    status_int = _hex_or_int(status) if status is not None else None
    checks.append(
        Check(
            "receipt.status == 1",
            status_int == 1,
            1,
            status_int,
            "eth_getTransactionReceipt",
            "Outer status only; see the caveat. The log checks below are what "
            "establish the inner call ran.",
        )
    )

    required = {"hash", "chainId", "from", "to", "input", "value"}
    valid_tx = isinstance(tx, dict) and required <= tx.keys()
    checks.append(Check("transaction required fields", valid_tx, sorted(required),
                        sorted(tx) if isinstance(tx, dict) else None, "eth_getTransactionByHash"))
    if not valid_tx:
        return VerificationResult(False, "sponsored", tuple(checks),
                                  ("transaction missing or incomplete",), tuple(sources), (exclusivity_caveat(),))
    checks.append(Check("tx.hash", str(tx["hash"]).lower() == txn_hash.lower()
                        and bool(re.fullmatch(r"0x[0-9a-fA-F]{64}", txn_hash)),
                        txn_hash, tx["hash"], "eth_getTransactionByHash"))
    checks.append(Check("tx.chainId", _hex_or_int(tx["chainId"]) == want.chain_id,
                        want.chain_id, tx["chainId"], "eth_getTransactionByHash"))
    for key in ("from", "to"):
        to_checksum_address(tx[key])  # malformed addresses fail at the public boundary
    if not isinstance(tx["input"], str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", tx["input"]):
        raise ValueError("malformed transaction input")
    checks.append(Check("wrapper native value == 0", _hex_or_int(tx["value"]) == 0,
                        0, tx["value"], "eth_getTransactionByHash"))
    for key in ("from", "to", "blockNumber", "blockHash", "transactionIndex"):
        if key in receipt and key in tx:
            left, right = receipt[key], tx[key]
            if key in {"blockNumber", "transactionIndex"}:
                left, right = _hex_or_int(left), _hex_or_int(right)
            else:
                left, right = str(left).lower(), str(right).lower()
            checks.append(Check(f"receipt.{key} == tx.{key}", left == right,
                                right, left, "RPC transaction and receipt"))

    # The top level must NOT be our own direct call: if it is, the caller
    # should be using the direct profile, whose comparison is strictly stronger.
    if tx:
        top_from = str(tx.get("from", "")).lower()
        top_to = str(tx.get("to") or "").lower()
        checks.append(
            Check(
                "top-level sender is not our wallet (a relayer)",
                top_from != wallet.lower(),
                f"!= {wallet}",
                tx.get("from"),
                "eth_getTransactionByHash",
                "A sponsored send is submitted by a relayer. If our wallet is "
                "the top-level sender this is a direct send and must be "
                "verified with the direct profile instead.",
            )
        )
        checks.append(
            Check(
                "top-level recipient is the wrapper, not the target",
                top_to != want.target.lower(),
                f"!= {want.target}",
                tx.get("to"),
                "eth_getTransactionByHash",
            )
        )

    # --- content check: the chain's own event logs -------------------------
    try:
        events = decode_event_logs(
            receipt, relevant_emitters={address for address in
                (want.target, want.asset, want.a_token) if address is not None}
        )
    except MalformedLog as exc:
        checks.append(Check("receipt logs decodable", False, "well-formed logs", str(exc), "receipt.logs"))
        blockers.append(f"receipt logs are malformed: {exc}")
        return VerificationResult(False, "sponsored", tuple(checks), tuple(blockers), tuple(sources), (exclusivity_caveat(), outer_status_caveat()))
    checks.append(Check("receipt logs decodable", True, "well-formed logs", f"{len(events)} recognised", "receipt.logs"))

    matcher = _MATCHERS.get(want.kind)
    if matcher is None:
        blockers.append(f"no effect matcher for kind={want.kind!r}; refusing to assert an effect this module cannot check")
        return VerificationResult(False, "sponsored", tuple(checks), tuple(blockers), tuple(sources), (exclusivity_caveat(), outer_status_caveat()))
    checks.extend(matcher(events, want))

    # --- corroboration: KeeperHub's own reconstruction ---------------------
    if executed_call is None:
        checks.append(
            Check(
                "executedCall corroboration",
                True,
                "present or absent",
                "absent",
                "keeperhub node output",
                "KeeperHub supplied no executedCall (the RPC could not trace "
                "the transaction). Not a failure: its origin is KeeperHub, and "
                "chain logs are checked here. Operation ownership must be "
                "checked by the integrating gate, outside this function.",
            )
        )
        blockers_soft = "executedCall absent: no trace corroboration; operation binding checked separately"
        caveats_extra = (blockers_soft,)
    else:
        sources.append("keeperhub: executedCall (trace)")
        caveats_extra = ()
        addr = str(executed_call.get("contractAddress", "")).lower()
        checks.append(Check("executedCall.contractAddress == target", addr == want.target.lower(), want.target, executed_call.get("contractAddress"), "keeperhub executedCall"))
        expected_function = expected_function or {"erc20_mint": "mint"}.get(want.kind, want.kind)
        if expected_function:
            fn = executed_call.get("functionName")
            checks.append(Check("executedCall.functionName", fn == expected_function, expected_function, fn, "keeperhub executedCall"))
        reverted = executed_call.get("reverted")
        checks.append(Check("executedCall.reverted is False", reverted is False, False, reverted, "keeperhub executedCall"))
        checks.append(
            Check(
                "executedCall.sponsored is True",
                executed_call.get("sponsored") is True,
                True,
                executed_call.get("sponsored"),
                "keeperhub executedCall",
            )
        )
        top_level_to = str(executed_call.get("topLevelTo", "")).lower()
        checks.append(Check("executedCall.topLevelTo != target", bool(top_level_to) and top_level_to != want.target.lower(), f"!= {want.target}", executed_call.get("topLevelTo"), "keeperhub executedCall"))
        checks.append(Check("executedCall.topLevelTo == tx.to", top_level_to == str(tx["to"]).lower(),
                            tx["to"], executed_call.get("topLevelTo"), "keeperhub trace and RPC"))
        checks.extend(_executed_call_args(executed_call, want))

    ok = all(c.passed for c in checks)
    if not ok:
        blockers.extend(f"{c.name}: expected {c.expected!r}, observed {c.observed!r}" for c in checks if not c.passed)
    return VerificationResult(
        ok,
        "sponsored",
        tuple(checks),
        tuple(blockers),
        tuple(sources),
        (exclusivity_caveat(), outer_status_caveat()) + caveats_extra,
    )


def _executed_call_args(executed_call: dict[str, Any], want: EffectExpectation) -> list[Check]:
    """Compare executedCall's decoded args against what we authorized.

    ``args`` is a name-keyed map of *stringified* values
    (``lib/web3/trace-decode.ts`` ``serializeArg``), so every comparison here is
    string-normalised on both sides rather than assuming a type.
    """
    checks: list[Check] = []
    args = executed_call.get("args")
    if not isinstance(args, dict) or not args:
        checks.append(Check("executedCall.args present", False, "a non-empty map", args, "keeperhub executedCall"))
        return checks
    lowered = {k.lower(): str(v) for k, v in args.items()}
    if len(lowered) != len(args):
        checks.append(Check("executedCall.args unique names", False, "unique names", list(args), "keeperhub trace"))

    def cmp(field_names: tuple[str, ...], expected: Any, label: str, is_address: bool) -> None:
        found = False
        for name in field_names:
            if name in lowered:
                found = True
                got = lowered[name]
                if is_address:
                    passed = got.lower() == str(expected).lower()
                else:
                    passed = _as_int(got) == expected
                checks.append(Check(f"executedCall.args.{name} ({label})", passed, expected, got, "keeperhub executedCall"))
        if found:
            return
        checks.append(Check(f"executedCall.args.{label}", False, f"one of {field_names}", sorted(lowered), "keeperhub executedCall", "argument not present under any expected name"))

    if want.kind == "supply":
        cmp(("asset", "reserve"), want.asset, "asset", True)
        cmp(("amount",), want.amount, "amount", False)
        cmp(("onbehalfof",), want.wallet, "onBehalfOf", True)
        cmp(("referralcode",), want.referral_code, "referralCode", False)
    elif want.kind == "withdraw":
        cmp(("asset", "reserve"), want.asset, "asset", True)
        cmp(("to",), want.recipient or want.wallet, "to", True)
        cmp(("amount",), want.amount, "amount", False)
    elif want.kind == "approve":
        cmp(("spender",), want.spender, "spender", True)
        cmp(("amount", "value", "_value"), want.amount, "amount", False)
    elif want.kind == "erc20_mint":
        cmp(("token",), want.asset, "token", True)
        cmp(("to",), want.wallet, "to", True)
        cmp(("amount",), want.amount, "amount", False)
    return checks


def _as_int(value: str) -> int | None:
    try:
        return int(value, 16) if value.startswith("0x") else int(value)
    except (TypeError, ValueError):
        return None


def verify_sponsored_effect(tx, receipt, expectation, *, rpc_chain_id, txn_hash,
                            wallet_address, executed_call=None, expected_function=None) -> VerificationResult:
    """Effect verification only. Does not assert an operation-id/hash binding."""
    try:
        return _verify_sponsored_effect(tx, receipt, expectation, rpc_chain_id=rpc_chain_id,
            txn_hash=txn_hash, wallet_address=wallet_address, executed_call=executed_call,
            expected_function=expected_function)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return VerificationResult(False, "sponsored", (), (f"malformed verification input: {exc}",),
                                  (), (exclusivity_caveat(), outer_status_caveat()))
