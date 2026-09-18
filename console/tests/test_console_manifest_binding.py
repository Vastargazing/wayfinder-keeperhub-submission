"""C2 path and saved identity controls, including every manifest consumer."""
import json
import shutil

import pytest
import runbuilder
from operator_console.model import build_view, UNDECLARED
from operator_console.render import render_page
from operator_console.sources import load_state_directory
from test_console_lifecycle import rewrite


def bundle(path, run_id=runbuilder.RUN_ID):
    steps = [(key.replace(runbuilder.RUN_ID, run_id), *tail) for key, *tail in runbuilder.DEFAULT_STEPS]
    root = runbuilder.write_run(path, run_id=run_id, steps=steps)
    p=root/'run-manifest.json'
    d=json.loads(p.read_bytes())
    d.update(stateDir='.', run_id=run_id, mode_labels={'chain':'TESTNET FOREIGN','signer':'TURNKEY FOREIGN'},
             position={'block':'FOREIGN-POSITION'}, not_proven=['FOREIGN-LIMIT'], workflowId='FOREIGN-WORKFLOW',
             wallet='FOREIGN-WALLET', keeperhub='FOREIGN-BASE', rpc='FOREIGN-RPC', fork_block='FOREIGN-BLOCK',
             corroboration={steps[0][0]:[{'label':'FOREIGN-CORROBORATION','value':'FOREIGN-VALUE'}]})
    p.write_text(json.dumps(d))
    return root, p


def refused(root, manifest):
    state=load_state_directory(root, manifest)
    assert not state.manifest.bound, state.manifest.binding_detail
    assert not state.manifest.mode_labels and not state.manifest.position
    assert not state.manifest.corroboration and not state.manifest.not_proven
    view=build_view(state)
    assert all(f.value == UNDECLARED for f in view.mode_facts)
    assert not view.not_proven and not view.unattached_corroboration
    assert not any(s.corroboration for s in view.steps)
    page=render_page(view)
    assert 'FOREIGN-' not in page
    return state


def test_same_suffix_in_two_existing_roots_is_not_identity(tmp_path):
    a, manifest = bundle(tmp_path/'A/state/run')
    b, _ = bundle(tmp_path/'B/state/run', 'b'*32)
    data=json.loads(manifest.read_bytes()); data['stateDir']=str(a); manifest.write_text(json.dumps(data))
    refused(b, manifest)


@pytest.mark.parametrize('kind', ['manifest_id','checkpoint_id','checkpoint_and_manifest_id','legacy'])
def test_exact_path_never_overrides_identity_problem(tmp_path, kind):
    root, manifest=bundle(tmp_path/'run')
    data=json.loads(manifest.read_bytes())
    if kind in {'manifest_id','checkpoint_and_manifest_id'}: data['run_id']='b'*32
    if kind=='legacy': del data['run_id']
    manifest.write_text(json.dumps(data))
    if kind in {'checkpoint_id','checkpoint_and_manifest_id'}:
        def change(d):
            d['calls']={k.replace(d['run_id'],'b'*32):v for k,v in d['calls'].items()};d['run_id']='b'*32
        rewrite(root/'run-plan.json', change)
    refused(root, manifest)


def test_relative_path_is_manifest_relative_and_survives_bundle_move(tmp_path, monkeypatch):
    root, manifest=bundle(tmp_path/'A/state/run')
    manifests=tmp_path/'A/metadata';manifests.mkdir()
    data=json.loads(manifest.read_bytes()); data['stateDir']='../state/run'
    manifest=manifests/'manifest.json';manifest.write_text(json.dumps(data))
    monkeypatch.chdir(tmp_path)
    before=load_state_directory(root,manifest)
    assert before.manifest.bound
    shutil.copytree(tmp_path/'A',tmp_path/'B')
    after=load_state_directory(tmp_path/'B/state/run',tmp_path/'B/metadata/manifest.json')
    assert after.manifest.bound
    view=build_view(after)
    assert any(f.value=='TESTNET FOREIGN' for f in view.mode_facts)
    assert view.unattached_corroboration and view.not_proven
    assert 'declaration' in after.manifest.binding_detail
