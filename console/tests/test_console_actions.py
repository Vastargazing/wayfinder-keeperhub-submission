"""The interface must be incapable of causing a second send.

These are the tests the interaction rule exists for: no send-capable action and
no retry affordance anywhere, not even disabled or behind a confirmation, and
"check state" as the primary action whenever an outcome is indeterminate.
"""

import re

import pytest

from operator_console.actions import ACTIONS, ALLOWED_HREFS, Action, CHECK_STATE
from operator_console.audit import (
    FORBIDDEN_ACTION_WORDS,
    FORBIDDEN_ATTRIBUTES,
    FORBIDDEN_ELEMENTS,
    SendAffordanceFound,
    audit_document,
)
from operator_console.model import Outcome
from operator_console.render import primary_action, render_page

HREF_RE = re.compile(r'href="([^"]*)"')
ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def test_every_action_in_the_registry_is_a_read():
    assert set(ACTIONS) == {"check-state", "reread"}
    for action in ACTIONS.values():
        assert action.method == "GET"
        assert action.kind == "read"
        assert action.href in ALLOWED_HREFS


def test_a_non_read_action_cannot_be_constructed():
    with pytest.raises(ValueError, match="not a read"):
        Action("resend", "Resend", "/", "resend it", kind="write")
    with pytest.raises(ValueError, match="not a read"):
        Action("resend", "Resend", "/", "resend it", method="POST")
    with pytest.raises(ValueError, match="not a read route"):
        Action("resend", "Resend", "/resend", "resend it")


def test_rendered_page_has_no_interactive_controls_and_no_script(page):
    lowered = page.lower()
    for element in FORBIDDEN_ELEMENTS:
        assert element not in lowered, element
    for attribute in FORBIDDEN_ATTRIBUTES:
        assert attribute not in lowered, attribute


def test_every_link_on_the_page_is_a_read_route(page):
    hrefs = {h for h in HREF_RE.findall(page) if not h.startswith("#")}
    assert hrefs
    assert hrefs <= set(ALLOWED_HREFS), hrefs


def test_no_clickable_element_offers_to_send_anything(page):
    for anchor in ANCHOR_RE.findall(page):
        text = TAG_RE.sub(" ", anchor).lower()
        for word in FORBIDDEN_ACTION_WORDS:
            assert word not in text, (word, text)


def test_indeterminate_offers_only_check_state(view):
    """The interaction rule, stated as the things it means.

    The page is rendered inside the test rather than taken from the fixture so
    that a renderer which grew a send affordance fails this test instead of
    erroring during setup.
    """
    unknown = [o for o in view.operations if o.finding.outcome is Outcome.INDETERMINATE]
    assert unknown, "the fixture run must contain an indeterminate operation"

    assert primary_action(view) is CHECK_STATE
    assert CHECK_STATE.label == "Check state"

    first = view.needs[0]
    assert first.action is CHECK_STATE
    assert first.subject == unknown[0].operation_id

    page = render_page(view)

    # Rendered: a primary action reading "Check state", and no other kind of link.
    primary = re.findall(r'<a class="action action--primary"[^>]*>(.*?)</a>', page)
    assert primary, "an indeterminate outcome must render a primary action"
    assert set(primary) == {"Check state"}
    labels = {TAG_RE.sub(" ", a).strip() for a in ANCHOR_RE.findall(page)}
    assert labels == {"Check state"}, labels


def test_audit_rejects_a_retry_link_positive_control():
    """The audit is load-bearing, so prove it fails on the thing it forbids."""
    for offender in [
        '<a class="action" href="/?resend=abc">Retry send</a>',
        '<a class="action" href="/">Re-submit this operation</a>',
        '<form method="post" action="/resend"></form>',
        '<button onclick="resend()">go</button>',
    ]:
        with pytest.raises(SendAffordanceFound):
            audit_document(offender)


def test_audit_accepts_the_page_it_is_given(page):
    assert audit_document(page) is page


def test_a_page_that_grew_a_retry_action_is_never_returned(view, monkeypatch):
    """Even a renderer change cannot emit a send affordance: render_page audits."""
    import operator_console.render as render_module

    original = render_module._operation_card

    def with_retry(operation):
        return original(operation) + '<a class="action" href="/?retry=1">Retry send</a>'

    monkeypatch.setattr(render_module, "_operation_card", with_retry)
    with pytest.raises(SendAffordanceFound):
        render_page(view)
