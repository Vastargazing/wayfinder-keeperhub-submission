"""C1 acceptance connects actual driver files to model, headline and actions."""
import hashlib
import html
import json

import pytest

from lifecycle_support import driver_files
from operator_console.actions import CHECK_STATE
from operator_console.audit import audit_document
from operator_console.model import build_view
from operator_console.render import primary_action, render_page
from operator_console.sources import load_state_directory


def rewrite(path, mutate):
    stored = json.loads(path.read_bytes())
    mutate(stored['data'])
    stored['sha256'] = hashlib.sha256(json.dumps(stored['data'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    path.write_text(json.dumps(stored))


def inspect(root):
    state = load_state_directory(root)
    view = build_view(state)
    page = render_page(view)
    assert audit_document(page) == page
    assert html.escape(view.stop.headline) in page
    (root/'screen.html').write_text(page)
    return state, view, page


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario,reason,status', [
    ('expired', 'expired and no swap operation is bound', 'stopped'),
    ('unknown', 'records no expiry', 'uncertain'),
    ('interrupted_quote', 'request was interrupted', 'stopped'),
    ('seed', 'Seed is unfinished', 'stopped'),
    ('composite', 'Composite call', 'uncertain'),
    ('iteration', 'Iteration #1 is in_flight', 'uncertain'),
])
async def test_driver_refusal_is_in_headline(tmp_path, scenario, reason, status):
    root = tmp_path/scenario
    world, errors = await driver_files(root, scenario)
    state, view, page = inspect(root)
    assert view.stop.blocked, (view.stop, errors)
    assert 'Nothing is blocked' not in page
    assert reason in ' '.join([view.stop.headline, *view.stop.paragraphs])
    assert view.stop.status == status
    assert state.checkpoint.integrity == 'verified'
    assert any(f.label == 'Checkpoint integrity' for f in view.source_facts)
    assert primary_action(view) is CHECK_STATE
    assert 'Check state' in page and 'data-status="'+status+'"' in page
    assert set(o.state for o in view.operations) <= {'submitted', 'pending'}
    if scenario == 'expired':
        assert [s['step'] for s in world.sends] == ['borrow', 'wrap']
    if scenario == 'interrupted_quote':
        assert world.quotes == 1 and 'no automatic second quote' in errors[-1]
    if scenario == 'unknown':
        assert 'unverified' in view.stop.headline
        assert 'expired or validity unknown' in errors[-1]


@pytest.mark.asyncio
async def test_completed_driver_and_later_expiry_are_clear(tmp_path):
    root = tmp_path/'normal'
    world, errors = await driver_files(root)
    state, view, page = inspect(root)
    assert not errors and len(world.sends) == 7
    assert not view.stop.blocked and view.stop.status == 'clear'
    assert len(view.steps) == 7 and all(s.status.value == 'executed' for s in view.steps)
    later = build_view(state, now=world.expiry+1)
    assert not later.stop.blocked and not later.needs
    assert primary_action(later) is None
    assert 'not a statement that the strategy achieved its goal' in render_page(later)


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['checksum', 'structure', 'run_id'])
async def test_unusable_checkpoint_never_confirms_driver_steps(tmp_path, damage):
    root = tmp_path/damage
    await driver_files(root)
    path = root/'run-plan.json'
    if damage == 'checksum':
        stored = json.loads(path.read_bytes()); stored['sha256'] = 'invalid'
        path.write_text(json.dumps(stored))
    elif damage == 'structure':
        rewrite(path, lambda d: d.update(iterations=[None]))
    else:
        def other_run(d):
            old = d['run_id']; d['run_id'] = 'b'*32
            d['calls'] = {k.replace(old, d['run_id']):v for k,v in d['calls'].items()}
        rewrite(path, other_run)
    state, view, page = inspect(root)
    assert view.stop.blocked, view.stop
    assert view.stop.status == 'uncertain'
    assert 'unverified' in view.stop.headline
    assert not view.steps, 'untrusted checkpoint must not supply executed step claims'
    assert len(view.operations) == 7, 'journal remains independent evidence'
    assert primary_action(view) is CHECK_STATE
    reason = 'different runs' if damage == 'run_id' else ('mismatch' if damage == 'checksum' else 'malformed')
    assert reason in ' '.join(view.stop.paragraphs)
    assert reason in page


@pytest.mark.asyncio
async def test_informational_manifest_limits_do_not_block_completed_driver(tmp_path):
    root=tmp_path/'normal'
    await driver_files(root)
    state=load_state_directory(root)
    manifest=root/'manifest.json'
    manifest.write_text(json.dumps({'stateDir':'.', 'run_id':state.checkpoint.run_id,
                                  'not_proven':['No live Turnkey validation']}))
    state=load_state_directory(root, manifest)
    view=build_view(state)
    assert state.manifest.bound and view.not_proven
    assert not view.stop.blocked
    assert 'No live Turnkey validation' in render_page(view)
