"""The three-valued outcome, derived only from things this process read.

The seam's contract is ``landed(hash) / never_seen() / indeterminate(detail)``
and the screen shows exactly those three. A two-valued view would have to fold
"the executor never saw it" into "we do not know", and the whole point of the
project is that those two demand opposite actions.

The screen does not decide outcomes; it reports them from a reader. Two readers
exist here:

- :class:`JournalOutcomeReader` answers from the journal file alone. It is the
  default because it needs nothing but a file this process already opened
  read-only, and because the journal is the SDK's own authorization record.
- a caller may supply any :class:`OutcomeReader` — for instance one wired to
  ``KeeperHubExecutor.lookup`` — when a run's executor is reachable. Nothing in
  this module submits anything; a reader is only ever asked a question.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from .provenance import Provenance


class Outcome(str, Enum):
    LANDED = "landed"
    NEVER_SEEN = "never_seen"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class OutcomeFinding:
    """One answer about one operation, with why it is that answer."""

    outcome: Outcome
    txn_hash: str | None
    detail: str
    provenance: Provenance
    origin: str

    def __post_init__(self) -> None:
        if self.outcome is Outcome.LANDED and not self.txn_hash:
            raise ValueError("a landed finding must carry the transaction hash")
        if self.outcome is not Outcome.LANDED and self.txn_hash:
            raise ValueError(f"a {self.outcome.value} finding must not carry a hash")


class OutcomeReader(Protocol):
    """Anything that can answer 'what became of this operation?' by reading."""

    name: str
    caveat: str

    def finding(self, row: dict[str, Any]) -> OutcomeFinding: ...


class JournalOutcomeReader:
    """Answer from the SDK journal row, and say plainly what that cannot settle."""

    name = "SDK journal on disk"
    caveat = (
        "No executor lookup is configured for this console, so an operation the "
        "journal leaves pending is reported indeterminate. That is the honest "
        "answer from this artifact alone: the journal records that the executor "
        "was called and that no hash came back, which does not distinguish "
        "'never signed' from 'broadcast, then the recording was lost'."
    )

    def __init__(self, origin: str = "sdk-journal.sqlite:operations") -> None:
        self.origin = origin

    def finding(self, row: dict[str, Any]) -> OutcomeFinding:
        state = row.get("state")
        txn_hash = row.get("txn_hash")
        if state == "submitted" and txn_hash:
            return OutcomeFinding(
                Outcome.LANDED, txn_hash,
                "The journal binds this hash to this operation id: the executor "
                "returned it, or a lookup answered landed and the seam adopted it. "
                "A landed hash is not a successful transaction; the SDK verifies "
                "the receipt separately.",
                Provenance.OBSERVED, f"{self.origin}(state, txn_hash)",
            )
        if state == "orphaned" and txn_hash:
            return OutcomeFinding(
                Outcome.LANDED, txn_hash,
                "This operation landed but a newer operation began before anything "
                "claimed it, so the journal marked it orphaned. The hash is real; "
                "the binding to a step in the run is not settled.",
                Provenance.OBSERVED, f"{self.origin}(state='orphaned', txn_hash)",
            )
        if state == "never_seen":
            return OutcomeFinding(
                Outcome.NEVER_SEEN, None,
                "The executor asserted it never signed this operation id. Terminal: "
                "nothing reached the chain for it and the caller may send again.",
                Provenance.OBSERVED, f"{self.origin}(state='never_seen')",
            )
        if state == "pending":
            return OutcomeFinding(
                Outcome.INDETERMINATE, None,
                "The executor was called for this operation and no hash was recorded. "
                "From the journal alone the outcome is unknown: it may never have been "
                "signed, or it may have been broadcast before the recording was lost. "
                "The seam keeps refusing new sends for this sender and chain until a "
                "lookup can answer landed or never_seen.",
                Provenance.INFERRED, f"{self.origin}(state='pending')",
            )
        return OutcomeFinding(
            Outcome.INDETERMINATE, None,
            f"The journal row is in state {state!r}"
            + (" with no hash" if not txn_hash else "")
            + ", which the seam's own state table does not describe. It is reported "
            "unknown rather than read as either extreme.",
            Provenance.INFERRED, f"{self.origin}(state)",
        )


OUTCOME_TEXT = {
    Outcome.LANDED: "landed",
    Outcome.NEVER_SEEN: "never seen",
    Outcome.INDETERMINATE: "indeterminate",
}
