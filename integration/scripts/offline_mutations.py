#!/usr/bin/env python3
"""Run behavioral controls and targeted mutations in disposable source copies."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from process_control import run_process
from offline_checks import observed_command, module_roots

ROOT=Path(__file__).resolve().parents[2]
MUTATIONS=[
 ('drop-rejected-observation','hosted-sepolia/hosted_sepolia/recovery.py',
  '                redactions = []',
  '                return {"ok": False, "blockers": [str(exc)]}',
  'hosted-sepolia/tests/test_rejected_observations.py::test_rejected_derivation_survives_reopen_and_resume[wrong_mask]'),
 ('mask-stars-without-rule','integration/keeperhub_executor/provenance.py',
  'if not _equal(observed, transformed):',
  'if not _equal(observed, transformed) and not (isinstance(observed, str) and "*" in observed):',
  'hosted-sepolia/tests/test_provenance.py::test_corruption_refuses_without_journal_transition[mask]'),
 ('discard-duplicate-record-observation','integration/keeperhub_executor/executor.py',
  '        collect_execution_hashes(fresh, "logs.execution.transaction_hashes")',
  '        # mutant: same record id as listing, incorrectly discard fresh observation',
  'hosted-sepolia/tests/test_provenance.py::test_ten_old_hashes_cannot_overrule_fresh_record'),
 ('expected-value-as-witness','hosted-sepolia/hosted_sepolia/recovery.py',
  '                    executed_call=ec,',
  '                    executed_call={**ec, "args": {**ec["args"], "token": want.asset}} if ec and want.kind == "erc20_mint" and "*" in str(ec.get("args", {}).get("token", "")) else ec,',
  'hosted-sepolia/tests/test_provenance.py::test_redacted_trace_without_full_witness_refuses'),
 ('hash-observation-quorum','hosted-sepolia/hosted_sepolia/recovery.py',
  'or not binding.get("hashReferences")',
  'or len(binding.get("hashReferences", [])) < 2',
  'hosted-sepolia/tests/test_provenance.py::test_single_hash_observation_is_sufficient_when_checks_pass'),
 ('fresh-swap-quote','moonwell/moonwell_demo/durable.py',
  "                    return copy.deepcopy(row['result'])",
  "                    if once:\n                        return await original(*args, **kwargs)\n                    return copy.deepcopy(row['result'])",
  'moonwell/tests/test_lifecycle.py::test_recovery_boundaries[swap-response]'),
 ('fresh-allowance-decision','wayfinder/upstream/wayfinder_paths/core/utils/tokens.py',
  'decision = await run("allowance-decision", decide)',
  'decision = await decide()',
  'moonwell/tests/test_lifecycle.py::test_allowance_effect_is_not_operation_ownership[response]'),
 ('allowance-as-ownership','wayfinder/upstream/wayfinder_paths/core/utils/tokens.py',
  '        if not decision["approve"]:',
  '        if await get_token_allowance(token_address, chain_id, owner, spender) >= amount or not decision["approve"]:',
  'moonwell/tests/test_lifecycle.py::test_allowance_effect_is_not_operation_ownership[response]'),
 ('whole-swap-replay','moonwell/moonwell_demo/durable.py',
  'replay=False, composite=True, resume=self.resume_swap',
  'replay=True, composite=True, resume=self.resume_swap',
  'moonwell/tests/test_lifecycle.py::test_recovery_boundaries[swap-response]'),
 ('claim-before-validation','wayfinder/upstream/wayfinder_paths/core/utils/executor.py',
  '            self.journal.consume_claim(envelope, op_id, txn_hash)',
  '',
  'integration/tests/test_authorization.py::test_claim_revalidation_reopens_without_resend'),
 ('unconditional-consumption','wayfinder/upstream/wayfinder_paths/core/utils/executor.py',
  '" WHERE operation_id=? AND consumed=0"',
  '" WHERE operation_id=?"',
  'integration/tests/test_authorization.py::test_claim_consumption_race'),
 ('unfinished-before-begin','wayfinder/upstream/wayfinder_paths/core/utils/executor.py',
  '        if row is not None:\n            raise ExecutionOutcomeUnknownError(',
  '        if False:\n            raise ExecutionOutcomeUnknownError(',
  'integration/tests/test_authorization.py::test_unfinished_envelope_blocks_begin'),
 ('local-authorization','integration/keeperhub_executor/executor.py',
  'env = self.authorization.verify_record(operation_id, txn_hash, record)',
  'env = {"chainId": self.chain_id, "from": self.wallet_address, "to": record["contractAddress"], "data": encode_from_recorded_input(record), "value": ether_string_to_wei(record["ethValue"])}',
  'integration/tests/test_authorization.py::test_authorization_lifecycle[calldata-resolve]'),
 ('repeat-borrow','moonwell/moonwell_demo/durable.py',
  "                    return copy.deepcopy(row['result'])",
  "                    if name == 'borrow':\n                        with self.execution.step(key + '/mutant-repeat'):\n                            return await original(*args, **kwargs)\n                    return copy.deepcopy(row['result'])",
  'moonwell/tests/test_lifecycle.py::test_recovery_boundaries[wrap-response]'),
 ('fresh-execution','integration/keeperhub_executor/executor.py',
  '        collect_execution_hashes(fresh, "logs.execution.transaction_hashes")',
  '        # mutation: discard the fresh hash observation',
  'hosted-sepolia/tests/test_f1_f3.py::test_fresh_execution_contradiction_never_advances_journal[hash]'),
 ('step-identity','wayfinder/upstream/wayfinder_paths/core/utils/executor.py',
  'if row is None or row["envelope"] != envelope or row["digest"] != envelope_digest(envelope):',
  'if row is None:',
  'moonwell/tests/test_lifecycle.py::test_step_identity_and_equal_envelopes'),
 ('fresh-identity','integration/keeperhub_executor/executor.py',
  'if fresh.get("id") != exec_id or fresh.get("workflowId") != self.workflow_id:', 'if False:',
  'hosted-sepolia/tests/test_f1_f3.py::test_fresh_execution_contradiction_never_advances_journal[execution_id]'),
 ('cross-table','hosted-sepolia/hosted_sepolia/hosted.py',
  '        return classify_execution_mode(out, raw)',
  '        return classify_execution_mode(out, raw, status_output=self.direct_execution_status(execution_id))',
  'hosted-sepolia/tests/test_f1_f3.py::test_workflow_mode_does_not_request_direct_table'),
 ('foreign-abi','hosted-sepolia/hosted_sepolia/events.py','            if emitter not in emitters:', '            if False:',
  'hosted-sepolia/tests/test_f1_f3.py::test_unrelated_erc721_transfer_does_not_reject_supply'),
 ('event-matcher','hosted-sepolia/hosted_sepolia/verify.py','checks.extend(matcher(events, want))','checks.extend([])', 'hosted-sepolia/tests'),
 ('absent-mode','hosted-sepolia/hosted_sepolia/verify.py',
  'return ModeFinding(ExecutionMode.UNKNOWN, sources,\n                       "No explicit execution mode',
  'return ModeFinding(ExecutionMode.DIRECT, sources,\n                       "No explicit execution mode', 'hosted-sepolia/tests'),
 ('supply-owner','hosted-sepolia/hosted_sepolia/verify.py','f["onBehalfOf"].lower() == want.wallet.lower()', 'True', 'hosted-sepolia/tests'),
 ('conflicting-flags','hosted-sepolia/hosted_sepolia/verify.py','if len({flag for flag, _ in flags}) > 1:', 'if False:', 'hosted-sepolia/tests'),
 ('effect-gate','hosted-sepolia/hosted_sepolia/recovery.py','"ok": all(r["ok"] for r in results)', '"ok": True', 'hosted-sepolia/tests'),
 ('operator-retry-action','console/operator_console/render.py',
  '    action = ""\n    if outcome is Outcome.INDETERMINATE:\n        action = _action_link(CHECK_STATE, primary=True)\n',
  '    action = ""\n    if outcome is Outcome.INDETERMINATE:\n        action = _action_link(CHECK_STATE, primary=True) + (\n'
  '            f\'<a class="action" href="/?resend={operation.operation_id}">Retry send</a>\')\n',
  'console/tests/test_console_actions.py::test_indeterminate_offers_only_check_state'),
 ('chain-state-marks-step-done','console/operator_console/model.py',
  '    del corroboration  # named so its exclusion is greppable, not accidental\n    return bool(\n',
  '    if corroboration:\n        return True\n    return bool(\n',
  'console/tests/test_console_model.py::test_chain_state_cannot_mark_a_step_done'),
]


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--sdk',type=Path,default=ROOT/'wayfinder/upstream')
    ap.add_argument("--only", nargs="+", choices=[m[0] for m in MUTATIONS],
                    help="rerun selected controls/mutations after a focused change")
    ap.add_argument("--timeout", type=float, default=60,
                    help="per-control/mutant process timeout in seconds (default: 60)")
    args=ap.parse_args()
    if not 0 < args.timeout < float("inf"):
        ap.error("--timeout must be positive")
    selected = [m for m in MUTATIONS if not args.only or m[0] in args.only]
    args.sdk=args.sdk.resolve()
    if not (args.sdk/'wayfinder_paths').is_dir():
        ap.error(f'SDK source tree not found at {args.sdk}; pass --sdk /path/to/sdk (must contain wayfinder_paths)')
    args.out.mkdir(parents=True,exist_ok=True)
    results=[]
    exit_code=0
    for name,rel,old,new,selector in selected:
        prepared_at=time.monotonic()
        print(f'{name}: preparing', flush=True)
        with tempfile.TemporaryDirectory(prefix='mutation-') as td:
            tree=Path(td)
            for folder in ['integration','hosted-sepolia','moonwell','console']:
                shutil.copytree(ROOT/folder,tree/folder,ignore=shutil.ignore_patterns('__pycache__','state','evidence','*.egg-info'))
            shutil.copy(ROOT/'pytest.ini',tree/'pytest.ini')
            shutil.copytree(args.sdk/'wayfinder_paths',tree/'wayfinder/upstream/wayfinder_paths',ignore=shutil.ignore_patterns('__pycache__'))
            env={**os.environ,'PYTHONPATH':os.pathsep.join(str(tree/p) for p in ['integration','hosted-sepolia','moonwell','console','wayfinder/upstream'])}
            cmd=[sys.executable,'-m','pytest',selector,'-q','--tb=short','-p','no:cacheprovider']
            copy_seconds=time.monotonic()-prepared_at
            for mode in ['control','mutant']:
                if mode=='mutant':
                    path=tree/rel;source=path.read_text();assert source.count(old)==1,(name,'mutation anchor')
                    path.write_text(source.replace(old,new))
                    if name=='claim-before-validation':
                        source=path.read_text()
                        anchor='            op_id, txn_hash = claimed\n'
                        assert source.count(anchor)==1,(name,'early consumption anchor')
                        path.write_text(source.replace(anchor,anchor+'            self.journal.consume_claim(envelope, op_id, txn_hash)\n'))
                    # Avoid timestamp/size-based stale bytecode from the control.
                    for cache in tree.rglob('__pycache__'):
                        shutil.rmtree(cache)
                tag=f'{name}-{mode}'
                log_path=args.out/(tag+'.log')
                roots={key: tree / path.relative_to(ROOT) for key,path in module_roots(ROOT/'wayfinder/upstream').items()}
                arguments=cmd[1:]+['--junitxml='+str((args.out/(tag+'-junit.xml')).resolve())]
                command=observed_command(arguments,(args.out/(tag+'-origins.json')).resolve(),roots)
                inputs={str(p.relative_to(tree)):hashlib.sha256(p.read_bytes()).hexdigest() for p in tree.rglob('*.py')}
                (args.out/(tag+'-inputs.json')).write_text(json.dumps(inputs,indent=2))
                print(f'{tag}: running (budget {args.timeout}s)',flush=True)
                with log_path.open('w') as log:
                    outcome=run_process(command,cwd=tree,env=env,log=log,timeout=args.timeout)
                output=log_path.read_text()
                expected=0 if mode=='control' else 1
                import xml.etree.ElementTree as ET
                junit=args.out/(tag+'-junit.xml')
                suites=ET.parse(junit).getroot().findall('testsuite') if junit.exists() else []
                failures=sum(int(s.get('failures',0)) for s in suites)
                errors=sum(int(s.get('errors',0)) for s in suites)
                tested=sum(int(s.get('tests',0)) for s in suites)
                valid=(outcome['exit']==expected and tested>0 and errors==0
                       and (mode=='control' or failures>0)
                       and 'ImportError' not in output and 'ERROR collecting' not in output)
                if not valid and not exit_code:
                    exit_code=outcome['exit'] if outcome['exit'] not in (0,1) else 1
                results.append(dict(name=name,mode=mode,command=command,log=tag+'.log',
                                    copy_seconds=copy_seconds,valid=valid,junit_tests=tested,
                                    junit_failures=failures,junit_errors=errors,**outcome))
                # Preserve completed cases even if a later timeout interrupts the run.
                (args.out/'mutations.json').write_text(json.dumps(results,indent=2)+'\n')
                print(f'{tag}: exit {outcome["exit"]}, valid={valid}',flush=True)
    print(json.dumps({'controls':sum(r['mode']=='control' and r['valid'] for r in results),
                      'mutations_detected':sum(r['mode']=='mutant' and r['valid'] for r in results)},indent=2))
    return exit_code

if __name__=='__main__': sys.exit(main())
