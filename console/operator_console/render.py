"""Draw the operator screen.

One page, no script, no controls. Everything an operator can click is a plain
GET link, and every displayed value carries its provenance chip. The finished
document goes through :func:`operator_console.audit.audit_document` before it is
returned, so a page with a send affordance in it is never produced at all.
"""

from __future__ import annotations

import html
import time
from typing import Iterable

from .actions import Action, CHECK_STATE
from .audit import audit_document
from .model import (
    CONTRACT_NOTES,
    Corroboration,
    Need,
    OperationView,
    RunView,
    StepStatus,
    StepView,
)
from .outcome import OUTCOME_TEXT, Outcome
from .provenance import Fact

STATUS_CLASS = {
    StepStatus.EXECUTED: "ok",
    StepStatus.UNKNOWN: "unknown",
    StepStatus.NOT_BOUND: "warn",
    StepStatus.NOT_SENT: "neutral",
    StepStatus.INCOMPLETE: "warn",
    StepStatus.NOT_STARTED: "neutral",
}

OUTCOME_CLASS = {
    Outcome.LANDED: "ok",
    Outcome.NEVER_SEEN: "neutral",
    Outcome.INDETERMINATE: "unknown",
}


def primary_action(view: RunView) -> Action | None:
    """The one thing to do next. It is a read, or there is nothing to do."""
    if any(o.finding.outcome is Outcome.INDETERMINATE for o in view.operations):
        return CHECK_STATE
    if view.stop.blocked or not view.available:
        return CHECK_STATE
    return None


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def short_hash(value: str) -> str:
    """Shorten a hash for a dense table. The full value stays in the facts."""
    return f"{value[:10]}…{value[-8:]}" if len(value) > 22 else value


def render_page(view: RunView) -> str:
    parts = [
        "<title>Operator screen — Wayfinder × KeeperHub</title>",
        f"<style>{STYLE}</style>",
        _header(view),
        _fixture_banner(view),
        _problems(view),
        _stop(view),
        _needs(view),
        _modes(view),
        _steps(view),
        _operations(view),
        _corroboration(view),
        _sources(view),
        _contract(view),
        _footer(view),
    ]
    return audit_document("\n".join(p for p in parts if p))


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

def _header(view: RunView) -> str:
    strategy = next((f.value for f in view.run_facts if f.label == "Strategy"), "no run")
    run_id = next((f.value for f in view.run_facts if f.label == "Run id"), "")
    action = primary_action(view)
    checked = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(view.checked_at))
    return f"""
<header class="top">
  <div>
    <p class="eyebrow">Wayfinder × KeeperHub — operator screen (reads only)</p>
    <h1>{esc(strategy)}</h1>
    <p class="runid">{('run ' + esc(run_id)) if run_id else esc(view.state_dir)}</p>
  </div>
  <div class="topright">
    <p class="checked">sources re-read at {esc(checked)}</p>
    {_action_link(action, primary=True) if action else ''}
  </div>
</header>"""


def _fixture_banner(view: RunView) -> str:
    if not view.fixture_notice:
        return ""
    return f"""
<section class="banner banner--fixture">
  <h2>FIXTURE DATA — this is not a run</h2>
  <p>{esc(view.fixture_notice)}</p>
</section>"""


def _problems(view: RunView) -> str:
    if not view.problems:
        return ""
    items = "".join(f"<li>{esc(p)}</li>" for p in view.problems)
    return f"""
<section class="banner banner--problem">
  <h2>Read problems</h2>
  <ul>{items}</ul>
</section>"""


def _stop(view: RunView) -> str:
    body = "".join(f"<p>{esc(p)}</p>" for p in view.stop.paragraphs)
    tone = "stop--blocked" if view.stop.blocked else "stop--clear"
    return f"""
<section class="stop {tone}" data-status="{esc(view.stop.status)}">
  <h2 class="section-label">Where execution stopped</h2>
  <p class="headline">{esc(view.stop.headline)}</p>
  {body}
</section>"""


def _needs(view: RunView) -> str:
    if not view.needs:
        return """
<section class="panel">
  <h2 class="section-label">What is needed to continue</h2>
  <p>Nothing. No operation is waiting on a decision.</p>
</section>"""
    items = []
    for index, need in enumerate(view.needs):
        items.append(_need_item(need, primary=index == 0 and need.action is not None))
    return f"""
<section class="panel">
  <h2 class="section-label">What is needed to continue</h2>
  <ol class="needs">{''.join(items)}</ol>
</section>"""


def _need_item(need: Need, *, primary: bool) -> str:
    subject = f'<p class="subject">{esc(need.subject)}</p>' if need.subject else ""
    action = _action_link(need.action, primary=primary) if need.action else ""
    return f"<li>{subject}<p>{esc(need.text)}</p>{action}</li>"


def _action_link(action: Action | None, *, primary: bool) -> str:
    if action is None:
        return ""
    css = "action action--primary" if primary else "action"
    return (
        f'<a class="{css}" href="{esc(action.href)}" '
        f'title="{esc(action.description)}">{esc(action.label)}</a>'
    )


def _modes(view: RunView) -> str:
    rows = "".join(_fact_row(f) for f in view.mode_facts)
    not_proven = ""
    if view.not_proven:
        items = "".join(f"<li>{esc(v)}</li>" for v in view.not_proven)
        not_proven = f'<div class="notproven"><h3>The run recorded these as not proven</h3><ul>{items}</ul></div>'
    return f"""
<section class="panel">
  <h2 class="section-label">Mode</h2>
  <p class="note">{esc(view.mode_note)}</p>
  <table class="facts">{rows}</table>
  {not_proven}
</section>"""


def _steps(view: RunView) -> str:
    if not view.steps:
        return """
<section class="panel">
  <h2 class="section-label">Money steps</h2>
  <p>No driver checkpoint was read as usable and belonging to this journal, so there is no ordered step view. The
     operations below are what the journal records, in the order they began.</p>
</section>"""
    rows = "".join(_step_row(index, step) for index, step in enumerate(view.steps, 1))
    return f"""
<section class="panel">
  <h2 class="section-label">Money steps, in order</h2>
  <p class="note">Ordered by when the journal began each step's operation.
     A step is executed only when the operation bound to its exact step id
     landed and the driver recorded the result.</p>
  <div class="tablewrap">
    <table class="steps">
      <tr><th>#</th><th>Step</th><th>Iter.</th><th>State</th><th>Operation</th><th>Outcome</th><th>Transaction</th></tr>
      {rows}
    </table>
  </div>
  {''.join(_step_detail(s) for s in view.steps)}
</section>"""


def _step_row(index: int, step: StepView) -> str:
    operation = step.operation
    outcome = (
        f'<span class="chip chip--{OUTCOME_CLASS[operation.finding.outcome]}">'
        f"{esc(OUTCOME_TEXT[operation.finding.outcome])}</span>"
        if operation else '<span class="chip chip--warn">no operation</span>'
    )
    txn = esc(short_hash(operation.txn_hash)) if operation and operation.txn_hash else "—"
    return f"""
    <tr>
      <td class="num">{index}</td>
      <td><span class="stepname">{esc(step.name)}</span><br><code>{esc(step.key)}</code></td>
      <td class="num">{esc(step.iteration if step.iteration is not None else '—')}</td>
      <td><span class="chip chip--{STATUS_CLASS[step.status]}">{esc(step.status.value)}</span></td>
      <td><code>{esc(operation.operation_id if operation else '—')}</code></td>
      <td>{outcome}</td>
      <td><code class="hash">{txn}</code></td>
    </tr>"""


def _step_detail(step: StepView) -> str:
    corroboration = _corroboration_table(
        step.corroboration,
        "Chain state beside this step — CORROBORATION, not proof of ownership",
    )
    needs = "".join(f"<li>{esc(n.text)}</li>" for n in step.needs)
    needs_html = f"<ul class='needs needs--inline'>{needs}</ul>" if needs else ""
    return f"""
  <div class="stepdetail">
    <h3>{esc(step.name)}</h3>
    <table class="facts">{''.join(_fact_row(f) for f in step.facts)}</table>
    {needs_html}
    {corroboration}
  </div>"""


def _operations(view: RunView) -> str:
    if not view.operations:
        return """
<section class="panel">
  <h2 class="section-label">Operations</h2>
  <p>The journal records no operations.</p>
</section>"""
    cards = "".join(_operation_card(o) for o in view.operations)
    return f"""
<section class="panel">
  <h2 class="section-label">Operations</h2>
  <p class="note">One card per row of the SDK journal, oldest first. Each is the
     authorization the SDK wrote before the executor was called.</p>
  <div class="cards">{cards}</div>
</section>"""


def _operation_card(operation: OperationView) -> str:
    outcome = operation.finding.outcome
    action = ""
    if outcome is Outcome.INDETERMINATE:
        action = _action_link(CHECK_STATE, primary=True)
    needs = "".join(f"<li>{esc(n.text)}</li>" for n in operation.needs)
    needs_html = f"<ul class='needs needs--inline'>{needs}</ul>" if needs else ""
    return f"""
  <article class="card card--{OUTCOME_CLASS[outcome]}">
    <div class="cardhead">
      <code class="opid">{esc(operation.operation_id)}</code>
      <span class="chip chip--{OUTCOME_CLASS[outcome]}">{esc(OUTCOME_TEXT[outcome])}</span>
    </div>
    <table class="facts">{''.join(_fact_row(f) for f in operation.facts)}</table>
    {needs_html}
    {action}
  </article>"""


def _corroboration(view: RunView) -> str:
    if not view.unattached_corroboration:
        return ""
    return f"""
<section class="panel">
  {_corroboration_table(view.unattached_corroboration,
                        'Chain state for the run — CORROBORATION, not proof of ownership')}
</section>"""


def _corroboration_table(entries: Iterable[Corroboration], title: str) -> str:
    entries = list(entries)
    if not entries:
        return ""
    rows = "".join(
        f"<tr><th>{esc(e.label)}</th><td>{esc(e.value)}</td>"
        f'<td><span class="prov prov--observed" data-prov="OBSERVED">OBSERVED</span>'
        f'<span class="origin">{esc(e.origin)}</span></td></tr>'
        for e in entries
    )
    return f"""
  <div class="corroboration">
    <h3>{esc(title)}</h3>
    <p class="note">A chain effect that looks right does not prove our operation
       produced it. Ownership comes only from the journal operation bound to a step.
       An unchanged nonce likewise does not prove nothing was sent.</p>
    <table class="facts">{rows}</table>
  </div>"""


def _sources(view: RunView) -> str:
    rows = "".join(_fact_row(f) for f in view.source_facts)
    probed = "".join(f"<li><code>{esc(p)}</code></li>" for p in view.probed)
    return f"""
<section class="panel">
  <h2 class="section-label">Sources</h2>
  <table class="facts">{rows}</table>
  <h3>Paths read</h3>
  <ul class="paths">{probed}</ul>
  <p class="note"><strong>{esc(view.reader_name)}</strong> answered every outcome on
     this page. {esc(view.reader_caveat)}</p>
</section>"""


def _contract(view: RunView) -> str:
    rows = "".join(_fact_row(f) for f in CONTRACT_NOTES)
    run_rows = "".join(_fact_row(f) for f in view.run_facts)
    return f"""
<section class="panel">
  <h2 class="section-label">The run</h2>
  <table class="facts">{run_rows}</table>
  <h3>The contract this screen reports against</h3>
  <table class="facts">{rows}</table>
</section>"""


def _footer(view: RunView) -> str:
    return """
<footer class="foot">
  <p><strong>This screen performs reads only.</strong> It opens the journal with
     SQLite's read-only mode, never writes the journal or the checkpoint, and has
     no retry, resend or re-submit action — not disabled, not behind a
     confirmation, not present. When an outcome is unknown the only thing it
     offers is checking state.</p>
  <p>Resolving a blocked operation is a separate, deliberate command that writes:
     <code>moonwell/scripts/operator_resolve.py</code>. It accepts a hash only when
     KeeperHub's own record binds that hash to the operation id and the five
     journalled envelope fields match, and it re-runs both checks at the moment it
     writes.</p>
</footer>"""


def _fact_row(fact: Fact) -> str:
    note = f'<span class="factnote">{esc(fact.note)}</span>' if fact.note else ""
    return (
        f"<tr class='fact'><th>{esc(fact.label)}</th>"
        f"<td class='factvalue'>{esc(fact.value)}{note}</td>"
        f"<td class='factprov'>"
        f"<span class='prov prov--{fact.provenance.value.lower()}' "
        f"data-prov='{esc(fact.provenance.value)}'>{esc(fact.provenance.value)}</span>"
        f"<span class='origin'>{esc(fact.origin)}</span></td></tr>"
    )


STYLE = """
:root {
  --bg: #f6f6f4; --panel: #ffffff; --ink: #1b1d21; --muted: #5c6169;
  --line: #dfe1e5; --ok: #1f7a4d; --okbg: #e7f4ec; --unknown: #9a4a06;
  --unknownbg: #fdf0e3; --warn: #8a1f2f; --warnbg: #fbeaec; --neutral: #46505c;
  --neutralbg: #eceff3; --fixture: #6a1b9a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161a; --panel: #1c1f25; --ink: #eceef2; --muted: #a0a7b3;
    --line: #2c3038; --ok: #6fd39b; --okbg: #14301f; --unknown: #f0ac63;
    --unknownbg: #33220f; --warn: #f08a98; --warnbg: #35171c; --neutral: #b6bec9;
    --neutralbg: #23272e; --fixture: #d69bf5;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
code { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace; font-size: .86em; }
header.top, section, footer { max-width: 1120px; margin: 0 auto; padding: 0 24px; }
header.top { display: flex; justify-content: space-between; align-items: flex-start;
  gap: 24px; padding-top: 28px; padding-bottom: 18px; flex-wrap: wrap; }
.eyebrow { margin: 0 0 6px; text-transform: uppercase; letter-spacing: .09em;
  font-size: 11px; color: var(--muted); }
h1 { margin: 0; font-size: 26px; letter-spacing: -.01em; }
.runid { margin: 4px 0 0; color: var(--muted); font-family: ui-monospace, monospace; font-size: 13px; }
.topright { text-align: right; }
.checked { margin: 0 0 8px; color: var(--muted); font-size: 12px; }
section { margin-bottom: 20px; }
.panel, .stop, .banner { background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 20px 24px; }
.section-label { margin: 0 0 12px; font-size: 12px; text-transform: uppercase;
  letter-spacing: .1em; color: var(--muted); font-weight: 600; }
h3 { font-size: 13px; text-transform: uppercase; letter-spacing: .07em;
  color: var(--muted); margin: 18px 0 8px; }
.stop { border-left: 4px solid var(--neutral); }
.stop--blocked { border-left-color: var(--unknown); }
.stop--clear { border-left-color: var(--ok); }
.headline { font-size: 21px; font-weight: 600; margin: 0 0 10px; letter-spacing: -.01em; }
.stop p { margin: 0 0 8px; max-width: 76ch; }
.banner--fixture { border: 2px solid var(--fixture); }
.banner--fixture h2 { color: var(--fixture); margin: 0 0 6px; font-size: 16px; letter-spacing: .04em; }
.banner--problem { border: 2px solid var(--warn); }
.banner--problem h2 { color: var(--warn); margin: 0 0 6px; font-size: 16px; }
.needs { margin: 0; padding-left: 22px; }
.needs li { margin-bottom: 14px; max-width: 84ch; }
.needs--inline { padding-left: 18px; margin: 10px 0 0; font-size: 13px; color: var(--muted); }
.subject { margin: 0 0 2px; font-family: ui-monospace, monospace; font-size: 12px; color: var(--muted); }
.action { display: inline-block; margin-top: 8px; padding: 7px 14px; border-radius: 7px;
  border: 1px solid var(--line); color: var(--ink); text-decoration: none; font-size: 13px;
  font-weight: 600; background: var(--neutralbg); }
.action--primary { background: var(--ink); color: var(--panel); border-color: var(--ink); }
table { border-collapse: collapse; width: 100%; }
.facts th { text-align: left; font-weight: 500; color: var(--muted); width: 210px;
  vertical-align: top; padding: 5px 12px 5px 0; font-size: 13px; }
.facts td { vertical-align: top; padding: 5px 0; font-size: 13px; }
.facts tr + tr th, .facts tr + tr td { border-top: 1px solid var(--line); }
.factvalue { word-break: break-word; }
.factnote { display: block; color: var(--muted); font-size: 12px; margin-top: 2px; max-width: 72ch; }
.factprov { width: 300px; text-align: right; }
.prov { display: inline-block; padding: 1px 7px; border-radius: 4px; font-size: 10px;
  font-weight: 700; letter-spacing: .07em; background: var(--neutralbg); color: var(--neutral); }
.prov--observed { background: var(--okbg); color: var(--ok); }
.prov--inferred { background: var(--unknownbg); color: var(--unknown); }
.prov--unverified { background: var(--warnbg); color: var(--warn); }
.origin { display: block; color: var(--muted); font-size: 10px; margin-top: 2px;
  font-family: ui-monospace, monospace; overflow-wrap: anywhere; }
.steps { font-size: 13px; }
.steps th { text-align: left; color: var(--muted); font-weight: 500; font-size: 11px;
  text-transform: uppercase; letter-spacing: .06em; padding: 0 10px 8px 0; }
.steps td { padding: 9px 10px 9px 0; border-top: 1px solid var(--line); vertical-align: top; }
.stepname { font-weight: 600; }
.steps code { color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
.steps td:nth-child(2) { min-width: 260px; }
.num { color: var(--muted); }
.tablewrap { overflow-x: auto; }
.hash { white-space: nowrap; }
.chip { display: inline-block; padding: 2px 9px; border-radius: 20px; font-size: 11px;
  font-weight: 700; white-space: nowrap; }
.chip--ok { background: var(--okbg); color: var(--ok); }
.chip--unknown { background: var(--unknownbg); color: var(--unknown); }
.chip--warn { background: var(--warnbg); color: var(--warn); }
.chip--neutral { background: var(--neutralbg); color: var(--neutral); }
.cards { display: grid; gap: 14px; }
.card { border: 1px solid var(--line); border-left: 4px solid var(--neutral);
  border-radius: 8px; padding: 14px 16px; }
.card--unknown { border-left-color: var(--unknown); }
.card--ok { border-left-color: var(--ok); }
.card--warn { border-left-color: var(--warn); }
.cardhead { display: flex; justify-content: space-between; align-items: center;
  gap: 12px; margin-bottom: 6px; }
.opid { font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.detail { margin: 0 0 10px; color: var(--muted); font-size: 13px; max-width: 82ch; }
.stepdetail { border-top: 1px solid var(--line); margin-top: 18px; padding-top: 10px; }
.corroboration { border: 1px dashed var(--line); border-radius: 8px; padding: 12px 14px; margin-top: 14px; }
.note { color: var(--muted); font-size: 13px; max-width: 86ch; }
.notproven ul, .paths { color: var(--muted); font-size: 13px; }
.foot { color: var(--muted); font-size: 13px; padding-bottom: 40px; max-width: 1120px; }
.foot p { max-width: 86ch; }
"""
