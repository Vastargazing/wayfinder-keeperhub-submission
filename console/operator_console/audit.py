"""Refuse to emit a page that could cause a second send.

The screen's one hard interaction rule is that it performs reads only. A rule
that lives in review comments is a rule that a later edit removes silently, so
it lives here instead: :func:`audit_document` runs over the finished HTML on
every render and raises rather than returning a page with a send affordance in
it.

The audit looks at interactive elements, not at prose. The page has to be able
to explain that there is no retry action; it must not be able to offer one.
"""

from __future__ import annotations

import re

from .actions import ALLOWED_HREFS

#: Elements that can send something, or run script that could.
FORBIDDEN_ELEMENTS = ("<form", "<button", "<input", "<textarea", "<select", "<script", "<iframe")

#: Attributes that turn a link or element into a submission.
FORBIDDEN_ATTRIBUTES = ("formaction=", "onclick=", "onsubmit=", "onchange=", "method=", "action=")

#: Words that must never appear in the text of something an operator can click.
FORBIDDEN_ACTION_WORDS = (
    "retry", "re-try", "resend", "re-send", "resubmit", "re-submit", "submit",
    "send", "broadcast", "sign", "execute", "force", "override", "attest",
    "approve", "confirm", "write", "resolve", "adopt", "publish",
)

HREF_RE = re.compile(r"href\s*=\s*\"([^\"]*)\"", re.IGNORECASE)
ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


class SendAffordanceFound(AssertionError):
    """The rendered page contains something that could cause a send."""


def audit_document(html: str) -> str:
    """Return ``html`` unchanged, or raise if it can do more than read."""
    lowered = html.lower()
    for element in FORBIDDEN_ELEMENTS:
        if element in lowered:
            raise SendAffordanceFound(
                f"the page contains {element!r}; this console renders no interactive "
                "controls and no script"
            )
    for attribute in FORBIDDEN_ATTRIBUTES:
        if attribute in lowered:
            raise SendAffordanceFound(
                f"the page contains the attribute {attribute!r}; only plain GET links are allowed"
            )
    for href in HREF_RE.findall(html):
        if href.startswith("#"):
            continue
        if href not in ALLOWED_HREFS:
            raise SendAffordanceFound(
                f"the page links to {href!r}, which is not one of this console's read routes "
                f"{ALLOWED_HREFS}"
            )
    for anchor in ANCHOR_RE.findall(html):
        text = TAG_RE.sub(" ", anchor).lower()
        for word in FORBIDDEN_ACTION_WORDS:
            if word in text:
                raise SendAffordanceFound(
                    f"a clickable element reads {text.strip()!r}, which offers to {word}; "
                    "the only action this console offers is checking state"
                )
    return html


#: Symbols whose presence in this package's own source would mean it can write.
FORBIDDEN_SOURCE_SYMBOLS = (
    "eth_sendTransaction",
    "eth_sendRawTransaction",
    "sendTransaction",
    "resolve_operation",
    "mark_resolved",
    "record_hash",
    "mark_never_seen",
    "consume_claim",
    "resolve_pending",
    "urllib.request",
    "httpx",
    "requests.",
    "socket.create_connection",
)


def audit_source(text: str) -> list[str]:
    """Names in ``text`` that would give this package a way to send or write."""
    return [symbol for symbol in FORBIDDEN_SOURCE_SYMBOLS if symbol in text]
