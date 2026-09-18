"""The complete set of things this screen can do.

It is a closed registry with one member that does anything, and that member
re-reads files. There is deliberately no retry, resend or re-submit action —
not disabled, not behind a confirmation, not present. An operator looking at an
operation whose fate is unknown is offered exactly one thing: check its state.

The constructor refuses to build anything that is not a read, so a send-capable
action cannot be added by accident; :mod:`operator_console.audit` then checks
the rendered page, because a link does not have to come from this registry to
appear in HTML.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Every URL the screen is allowed to link to. All are GET, all are reads.
ALLOWED_HREFS = ("/", "/?checked=1")


@dataclass(frozen=True)
class Action:
    """A read the operator can ask for. There is no other kind."""

    id: str
    label: str
    href: str
    description: str

    #: Fixed, not a parameter: an action that is not a read cannot be built.
    method: str = "GET"
    kind: str = "read"

    def __post_init__(self) -> None:
        if self.method != "GET" or self.kind != "read":
            raise ValueError(
                f"action {self.id!r} is not a read; this console has no non-read actions"
            )
        if self.href not in ALLOWED_HREFS:
            raise ValueError(f"action {self.id!r} links to {self.href!r}, which is not a read route")


CHECK_STATE = Action(
    id="check-state",
    label="Check state",
    href="/?checked=1",
    description=(
        "Re-read the journal, the driver checkpoint and the run manifest from disk "
        "and re-derive every outcome from what they say now. Nothing is sent."
    ),
)

REREAD = Action(
    id="reread",
    label="Check state",
    href="/",
    description="Re-read every source artifact and redraw this page. Nothing is sent.",
)

#: The whole interface surface.
ACTIONS: dict[str, Action] = {a.id: a for a in (CHECK_STATE, REREAD)}
