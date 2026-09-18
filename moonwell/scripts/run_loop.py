"""Drive `moonwell_wsteth_loop_strategy` through the seam, KeeperHub and a Base fork.

Scenarios
---------
``loop``      seed lend + the strategy's own `_loop_wsteth`, N iterations.
``crash``     the same run, hard-killed (`os._exit(137)`) at the moment KeeperHub
              has broadcast the *k*-th `borrow(uint256)` and this process knows
              the hash but the SDK's journal does not.
``resume``    replay the in-flight iteration from the driver checkpoint. Funds
              nothing, reverts nothing, snapshots nothing; the first thing it does
              after opening the journal is ask the executor.
``blocked``   the same run, but `docker kill keeperhub-executor` right after
              KeeperHub records a broadcast for the *k*-th borrow, so the
              execution row is left non-terminal forever and `lookup` is
              genuinely `indeterminate`.
``recheck``   re-run `resume` against a still-indeterminate operation: must send
              nothing at all.
``status``    read-only dump of an EXISTING run: journal + plan + chain position.
              It creates no state directory, no checkpoint, no lock and no
              journal; a run it cannot read is reported as unavailable with exit
              2. Its report is written outside the state directory it describes.

Every scenario writes one JSON evidence file and one log.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "integration"))
sys.path.insert(0, str(ROOT.parent / "wayfinder" / "upstream"))

from moonwell_demo import wiring  # noqa: E402
from moonwell_demo.chain import Chain  # noqa: E402
from moonwell_demo.labels import MODE, NOT_PROVEN  # noqa: E402
from moonwell_demo.durable import DurableIteration
from moonwell_demo.plan import RunPlan, CheckpointError  # noqa: E402
from moonwell_demo import status as run_status  # noqa: E402
from moonwell_demo.stubs import install_stubs  # noqa: E402
from moonwell_demo.wiring import BORROW_SELECTOR, CHAIN_ID, KH_BASE, RPC, WALLET  # noqa: E402

EV = ROOT / "evidence"
STATE = ROOT / "state"
WF_FILE = EV / "00-workflow-id.txt"


def docker(*args: str) -> str:
    out = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)
    return (out.stdout + out.stderr).strip()


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, kind: str, payload: dict) -> None:
        # `payload` may itself carry a "kind" (the execute outcome: started /
        # replay / in_progress / conflict), so keep both rather than letting one
        # silently shadow the other.
        event = {**payload, "kind": kind, "at": time.time()}
        if "kind" in payload:
            event["outcome_kind"] = payload["kind"]
        self.events.append(event)
        if kind == "prepare":
            d = payload["decoded"]
            logger.info(
                f"[exec] prepare op={payload['operationId']} {d['signature']} "
                f"sel={d['selector']} byte_equal={d['byte_equal']} calldata={d['calldata_len_bytes']}B"
            )
        elif kind == "execute":
            logger.info(
                f"[exec] execute op={payload['operationId']} -> {payload['kind']} "
                f"HTTP {payload['status']} execution={payload.get('executionId')}"
            )
        elif kind == "lookup":
            logger.info(
                f"[exec] lookup op={payload['operationId']} -> {payload['outcome']} "
                f"{payload.get('txnHash') or ''} :: {(payload.get('detail') or '')[:200]}"
            )
        elif kind == "preflight":
            logger.info(
                f"[exec] preflight sel={payload['selector']} #{payload['nth_for_selector']} "
                f"eth_call ok={payload.get('ok')} {str(payload.get('error'))[:160]}"
            )
        elif kind == "brap_stand_in_quote":
            logger.info(
                f"[STAND-IN BRAP] LI.FI tool={payload['tool']} router={payload['router']} "
                f"sel={payload['selector']} in={payload['input_amount']} out={payload['output_amount']} "
                f"slippage {payload['requested_slippage']}->{payload['effective_slippage']}"
            )
        elif kind == "borrowable":
            logger.info(
                f"[state-dependence] get_borrowable_amount -> {payload['ok']} "
                f"{payload['value']} (= ${payload['usd']:.2f})"
            )


async def continue_loop(strategy, *, log: bool = True):
    """Re-enter the strategy's own `_loop_wsteth` without re-seeding.

    Demo-layer glue: it recomputes exactly the arguments `_execute_deposit_loop`
    computes (strategy.py:2821-2864) minus the initial USDC lend, then hands off
    to the real loop. Nothing about the loop's own logic is reimplemented.
    """
    wsteth_price = await strategy._get_token_price(
        "superbridge-bridged-wsteth-base-base"
    )
    weth_price = await strategy._get_token_price("l2-standard-bridged-weth-base-base")
    cfs = await strategy._get_collateral_factors()
    weth_pos = await strategy.moonwell_adapter.get_pos(mtoken=wiring.M_WETH)
    current_borrowed_value = 0.0
    if weth_pos[0] and isinstance(weth_pos[1], dict):
        current_borrowed_value = (weth_pos[1].get("borrow_balance", 0) / 10**18) * weth_price
    usdc_v, wsteth_v, lev = await strategy._get_current_leverage(collateral_factors=cfs)
    if log:
        logger.info(
            f"[continue] usdc_collateral=${usdc_v:.2f} wsteth_collateral=${wsteth_v:.2f} "
            f"debt=${current_borrowed_value:.2f} leverage={lev:.3f} cf={cfs}"
        )
    return await strategy._loop_wsteth(
        wsteth_price=wsteth_price,
        weth_price=weth_price,
        current_borrowed_value=current_borrowed_value,
        initial_leverage=lev,
        usdc_lend_value=usdc_v,
        wsteth_lend_value=wsteth_v,
        collateral_factors=cfs,
    )


def instrument(strategy, plan: RunPlan, rec: Recorder, chain: Chain, *, max_iterations: int | None):
    """Wrap two real methods with recorders. Neither changes what they do.

    The iteration cap is applied by setting the strategy's own
    `_MAX_LOOP_LIMIT` (strategy.py:103, read by `for i in range(...)` at :2947),
    so the loop exits through its own normal path rather than by an injected
    exception.
    """
    if max_iterations is not None:
        strategy._MAX_LOOP_LIMIT = int(max_iterations)
    real_iter = strategy._atomic_deposit_iteration
    real_borrowable = strategy.moonwell_adapter.get_borrowable_amount
    last_borrowable = {"value": None}

    async def borrowable(*a, **kw):
        ok, val = await real_borrowable(*a, **kw)
        last_borrowable["value"] = val if ok else None
        rec("borrowable", {"ok": ok, "value": val, "usd": (val / 10**18) if ok and isinstance(val, int) else 0.0})
        return ok, val

    async def wrapped_iter(borrow_amt_wei: int):
        blk = await chain.block_number()
        pos = await chain.position(WALLET, hex(blk))
        if plan.data['seed'] and plan.data['seed']['state'] == 'intent':
            # The real deposit routine reached its loop only after seed succeeded.
            plan.finish_seed()
        r = plan.start_iteration(borrow_amt_wei, last_borrowable["value"])
        rec(
            "iteration_start",
            {
                "index": r.index,
                "borrow_amt_wei": int(borrow_amt_wei),
                "borrowable_wei_used": last_borrowable["value"],
                "position_before": pos,
            },
        )
        logger.info(
            f"[iteration {r.index}] borrow_amt_wei={borrow_amt_wei} "
            f"(from account liquidity {last_borrowable['value']})"
        )
        try:
            out = await real_iter(borrow_amt_wei)
        except BaseException as exc:  # noqa: BLE001
            plan.finish_iteration(None, f"failed: {type(exc).__name__}")
            rec("iteration_failed", {"index": r.index, "error": f"{type(exc).__name__}: {exc}"})
            raise
        plan.finish_iteration(int(out), "done")
        blk2 = await chain.block_number()
        rec(
            "iteration_done",
            {"index": r.index, "lend_amt_wei": int(out), "position_after": await chain.position(WALLET, hex(blk2))},
        )
        return out

    strategy._atomic_deposit_iteration = wrapped_iter
    strategy.moonwell_adapter.get_borrowable_amount = borrowable
    return real_iter


async def journal_evidence(journal, chain: Chain) -> list[dict]:
    """Every journal row, joined to what is actually on chain for it."""
    return await journal_rows_evidence(journal.entries(), chain)


async def journal_rows_evidence(rows: list[dict], chain: Chain) -> list[dict]:
    """The same join for rows already read, so `status` needs no journal writer."""
    out = []
    for row in rows:
        item = {
            "operation_id": row["operation_id"],
            "state": row["state"],
            "consumed": row["consumed"],
            "txn_hash": row["txn_hash"],
            "envelope": row["envelope"],
            "digest": row["digest"],
            "selector": (row["envelope"].get("data") or "0x")[:10],
        }
        if row["txn_hash"]:
            item["onchain"] = await chain.onchain_calldata_matches(row["txn_hash"], row["envelope"])
            if item["onchain"].get("blockNumber") is not None:
                item["position_after"] = await chain.position(WALLET, hex(item["onchain"]["blockNumber"]))
        out.append(item)
    return out


async def status_report(args) -> int:
    """Describe an existing run and write one report about it. Creates no run state.

    Returns the process exit code: 0 when the run was described, 2 when it could
    not be. Nothing on this path constructs ``RunPlan`` or ``OperationJournal``,
    so a missing, damaged or foreign run is reported rather than created: the
    writer constructors make a directory, take the lock, write a fresh checkpoint
    and create the database, which is exactly what an inspection must not do.

    Local sources are read first, so a run that cannot be described is refused
    without reaching for the workflow id or the chain. The fork/chain checks that
    follow are the same read-only observations this command always made.
    """
    state_dir = Path(args.state_dir or (STATE / args.scenario))
    out = Path(args.out) if args.out else EV / f"status-{state_dir.name}.json"
    try:
        out = run_status.resolve_output(out, state_dir)
    except run_status.StatusUnavailable as refusal:
        # The output path itself is the problem, so nothing is written anywhere.
        print(f"status unavailable: {refusal}", file=sys.stderr)
        return 2

    try:
        state = run_status.read_state(state_dir)
    except run_status.StatusUnavailable as refusal:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(
            run_status.unavailable_report(args.scenario, state_dir, out, refusal), indent=1))
        print(f"status unavailable: {refusal}", file=sys.stderr)
        print(f"refusal -> {out}", file=sys.stderr)
        return 2

    workflow_id = WF_FILE.read_text().strip() if WF_FILE.is_file() else None
    chain = Chain(RPC)
    ev: dict = {
        "scenario": args.scenario,
        "run_id": state.run_id,
        "mode_labels": MODE,
        "not_proven": NOT_PROVEN,
        "wallet": WALLET,
        "workflowId": workflow_id,
        "keeperhub": KH_BASE,
        "rpc": RPC,
        "chainId": CHAIN_ID,
        # Relative to this report's own directory, from the resolved paths, so the
        # state directory and its report can be moved together and still agree.
        "stateDir": run_status.manifest_state_dir(state_dir, out),
        "stateDirResolved": str(state_dir.resolve()),
        "checkpointSha256": state.checkpoint_sha256,
        "args": vars(args),
        "startedAt": time.time(),
    }
    ev["fork_block"] = await chain.fork_block()
    assert await chain.rpc("eth_chainId", []) == "0x2105", "not the Base fork"
    assert ev["fork_block"], "RPC is not an anvil fork; refusing"
    ev["position"] = await chain.position(WALLET)
    ev["plan"] = state.checkpoint
    ev["journal"] = await journal_rows_evidence(state.rows, chain)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ev, indent=1, default=str))
    print(json.dumps({"position": ev["position"], "rows": len(ev["journal"])}, indent=1))
    logger.info(f"[status] run {state.run_id} -> {out}")
    return 0


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=["loop", "crash", "resume", "blocked", "recheck", "status"])
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--out", required=False)
    ap.add_argument("--usdc", type=float, default=1000.0)
    ap.add_argument("--max-iterations", type=int, default=2)
    ap.add_argument("--crash-on-borrow", type=int, default=1,
                    help="crash/kill at the Nth borrow(uint256) of THIS process")
    ap.add_argument("--submit-timeout", type=float, default=180.0)
    ap.add_argument("--stub-lido", action="store_true")
    ap.add_argument("--skip-seed", action="store_true",
                    help="collateral already exists on chain: go straight into the strategy's "
                         "own _loop_wsteth instead of lending fresh USDC first")
    ap.add_argument("--continue-loop", action="store_true",
                    help="after resuming the in-flight iteration, re-enter _loop_wsteth")
    ap.add_argument("--restart-executor", action="store_true",
                    help="blocked scenario: docker start the executor again at the end")
    args = ap.parse_args()

    if args.scenario == "status":
        # Decided here, above every writer: the lines below create the state
        # directory, a checkpoint under an exclusive lock and an SDK journal.
        raise SystemExit(await status_report(args))

    EV.mkdir(parents=True, exist_ok=True)
    state_dir = Path(args.state_dir or (STATE / args.scenario))
    state_dir.mkdir(parents=True, exist_ok=True)
    workflow_id = WF_FILE.read_text().strip()
    chain = Chain(RPC)
    rec = Recorder()
    plan = RunPlan(state_dir / "run-plan.json")

    ev: dict = {
        "scenario": args.scenario,
        "mode_labels": MODE,
        "not_proven": NOT_PROVEN,
        "wallet": WALLET,
        "workflowId": workflow_id,
        "keeperhub": KH_BASE,
        "rpc": RPC,
        "chainId": CHAIN_ID,
        "stateDir": str(state_dir),
        "args": vars(args),
        "startedAt": time.time(),
    }
    ev["fork_block"] = await chain.fork_block()
    assert await chain.rpc("eth_chainId", []) == "0x2105", "not the Base fork"
    assert ev["fork_block"], "RPC is not an anvil fork; refusing"

    kill_state = {"done": False}

    def selector_crash(selector: str, payload: dict) -> None:
        """Fires inside submit(), BEFORE the execute POST."""
        if selector != BORROW_SELECTOR or payload["nth"] != args.crash_on_borrow:
            return
        if args.scenario == "blocked" and not kill_state["done"]:
            kill_state["done"] = True
            op = payload["operationId"]
            kill_state["op"] = op
            logger.error(f"[blocked] arming: will kill keeperhub-executor once it records a broadcast for {op}")

    def crash_hook(stage: str, payload: dict) -> None:
        sel = (executor.preflights[-1]["selector"] if executor.preflights else "")
        nth = (executor.preflights[-1]["nth_for_selector"] if executor.preflights else 0)
        if sel != BORROW_SELECTOR or nth != args.crash_on_borrow:
            return
        if args.scenario == "crash" and stage == "after_hash":
            logger.error(
                f"[crash] KeeperHub broadcast {payload['txnHash']} for borrow operation "
                f"{payload['operationId']}; killing THIS process before the SDK learns the hash"
            )
            (state_dir / "crash-boundary.json").write_text(
                json.dumps({"stage": stage, **payload, "events": rec.events}, indent=1, default=str)
            )
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(137)
        if args.scenario == "blocked" and stage == "after_execute_post" and kill_state.get("op"):
            op = kill_state["op"]
            assert op.isalnum() and workflow_id.isalnum()
            deadline = time.monotonic() + 60
            observed = ""
            while time.monotonic() < deadline:
                observed = docker(
                    "exec", "upstream-db-1", "psql", "-U", "postgres", "-d", "keeperhub", "-tA", "-c",
                    "select p.tx_hash from pending_transactions p join workflow_executions e "
                    f"on e.id=p.execution_id where e.workflow_id='{workflow_id}' "
                    f"and e.input->>'operationId'='{op}'",
                )
                if observed.startswith("0x"):
                    break
                time.sleep(0.1)
            if not observed.startswith("0x"):
                raise RuntimeError("blocked scenario: no recorded broadcast; kill boundary not reached")
            out = docker("kill", "keeperhub-executor")
            logger.error(
                f"[blocked] KeeperHub recorded broadcast {observed} for {op}; "
                f"docker kill keeperhub-executor -> {out!r}"
            )
            (state_dir / "blocked-boundary.json").write_text(
                json.dumps({"operationId": op, "pending_tx_hash": observed, "docker": out}, indent=1)
            )
            kill_state["op"] = None

    strategy, execution, executor, client, journal = wiring.build(
        workflow_id=workflow_id,
        state_dir=state_dir,
        on_evidence=rec,
        crash_hook=crash_hook,
        selector_crash=selector_crash,
        submit_timeout=args.submit_timeout,
    )
    stubs = install_stubs(on_evidence=rec, stub_lido=args.stub_lido, strategy=strategy)
    ev["abi_provenance"] = wiring.register_demo_abis()
    ev["journal_at_start"] = journal.entries()
    ev["plan_at_start"] = json.loads(json.dumps(plan.data))
    ev["before"] = await chain.position(WALLET)
    logger.info(f"[state] before: {json.dumps(ev['before'], default=str)}")

    if plan.created and journal.entries():
        raise CheckpointError("checkpoint missing but SDK journal is not empty")
    DurableIteration(strategy, execution, plan)
    real_iter = instrument(strategy, plan, rec, chain, max_iterations=args.max_iterations)
    await strategy.setup()

    try:
        if args.scenario in {"loop", "crash", "blocked"}:
            if plan.seed_done or args.skip_seed:
                if args.skip_seed and not plan.seed_done:
                    plan.record_seed(0.0)
                    plan.finish_seed(external=True)
                logger.info("[seed] skipped (collateral already on chain); re-entering _loop_wsteth")
                result = await continue_loop(strategy)
            else:
                plan.record_seed(args.usdc)
                result = await strategy._execute_deposit_loop(args.usdc)
                if result[0] and not plan.seed_done:
                    plan.finish_seed()
            ev["result"] = list(result)
            logger.info(f"[result] {result}")

        elif args.scenario in {"resume", "recheck"}:
            # `resume` replays the iteration whose result this driver never learned.
            # `recheck` replays the LAST iteration whatever its recorded status:
            # after an indeterminate outcome the wrapper marks the iteration
            # "failed", and the point of a recheck is precisely that attempting it
            # again must be refused before anything is sent.
            in_flight = plan.in_flight
            ev["in_flight_checkpoint"] = in_flight
            if in_flight is None:
                logger.warning("[resume] no in-flight iteration in the plan")
                ev["result"] = [False, "nothing in flight"]
            else:
                amt = int(in_flight["borrow_amt_wei"])
                logger.info(
                    f"[resume] replaying iteration {in_flight['index']} with the checkpointed "
                    f"borrow_amt_wei={amt} (recomputing it would produce a different envelope: "
                    "the borrow already changed account liquidity)"
                )
                blk = await chain.block_number()
                rec("resume_iteration_start", {
                    "index": in_flight["index"],
                    "borrow_amt_wei": amt,
                    "checkpointed_borrowable_wei": in_flight.get("borrowable_wei_before"),
                    "borrowable_now": (await strategy.moonwell_adapter.get_borrowable_amount())[1],
                    "position_before": await chain.position(WALLET, hex(blk)),
                })
                try:
                    out = await real_iter(amt)
                    plan.finish_iteration(int(out), "done")
                    ev["result"] = [True, out]
                    blk2 = await chain.block_number()
                    rec("resume_iteration_done", {
                        "index": in_flight["index"], "lend_amt_wei": int(out),
                        "position_after": await chain.position(WALLET, hex(blk2)),
                    })
                    logger.info(f"[resume] iteration completed, lend_amt_wei={out}")
                except (Exception, CheckpointError) as exc:  # noqa: BLE001
                    ev["result"] = [False, f"{type(exc).__name__}: {exc}"]
                    ev["resume_error"] = f"{type(exc).__name__}: {exc}"
                    logger.error(f"[resume] {type(exc).__name__}: {exc}")
                if args.continue_loop and ev["result"][0]:
                    ev["continue_result"] = list(await continue_loop(strategy))
                    logger.info(f"[continue] {ev['continue_result']}")
    finally:
        ev["submits_this_run"] = executor.submits
        ev["preflights"] = executor.preflights
        ev["after"] = await chain.position(WALLET)
        ev["journal"] = await journal_evidence(journal, chain)
        ev["plan"] = json.loads(json.dumps(plan.data))
        ev["events"] = rec.events
        ev["brap_stand_in_calls"] = [
            {k: v for k, v in c.items() if k != "raw_quote"} for c in stubs.lifi.calls
        ]
        ev["brap_stand_in_raw_quotes"] = [c["raw_quote"] for c in stubs.lifi.calls]
        ev["token_stub_calls"] = len(stubs.token.calls)
        ev["finishedAt"] = time.time()
        out = Path(args.out) if args.out else EV / f"{args.scenario}.json"
        out.write_text(json.dumps(ev, indent=1, default=str))
        logger.info(f"[state] after: {json.dumps(ev['after'], default=str)}")
        logger.info(f"submits this run: {executor.submits}")
        logger.info(f"evidence -> {out}")
        plan.close()
        await executor.close()
        await client.close()
        if args.restart_executor:
            logger.warning(f"[restore] docker start keeperhub-executor -> {docker('start', 'keeperhub-executor')!r}")

    # A requested resume must succeed as a shell command as well as in evidence.
    # Keep the deliberate recheck/crash/blocked demonstration contracts intact.
    # Decide only after evidence is saved and the writer/clients are closed.
    if args.scenario == "resume" and (
        ev["result"][0] is not True
        or ("continue_result" in ev and ev["continue_result"][0] is not True)
    ):
        raise SystemExit(1)


asyncio.run(main())
