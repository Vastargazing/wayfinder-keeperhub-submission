"""Actual lifecycle writers with modelled external calls, temporary state only."""
import copy
import importlib.util
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('console_lifecycle_model', ROOT / 'moonwell/tests/test_lifecycle.py')
life = importlib.util.module_from_spec(spec)
spec.loader.exec_module(life)


async def driver_files(root, scenario='normal'):
    root.mkdir(parents=True)
    world = life.World()
    if scenario == 'expired':
        world.expiry = 1
    if scenario == 'unknown':
        world.expiry = None
    errors = []
    with pytest.MonkeyPatch.context() as mp:
        strategy = await world.open(root, mp, start=scenario != 'seed')
        if scenario == 'seed':
            world.plan.record_seed(10)
        elif scenario != 'iteration':
            flush = world.plan._flush
            if scenario in {'interrupted_quote', 'composite', 'unknown'}:
                def interrupt():
                    calls = world.plan.data['calls']
                    if scenario == 'interrupted_quote' and any(k.endswith('/best_quote') and r['state'] == 'done' for k, r in calls.items()):
                        raise life.CheckpointError('quote result lost before checkpoint')
                    flush()
                    if scenario == 'unknown' and any(k.endswith('/best_quote') and r['state'] == 'done' for k, r in calls.items()):
                        raise life.CheckpointError('interrupted after quote was saved')
                    if scenario == 'composite' and any(r.get('composite') for r in calls.values()):
                        raise life.CheckpointError('interrupted after composite intent; no child')
                world.plan._flush = interrupt
            try:
                result = await strategy._atomic_deposit_iteration(life.AMOUNT)
                world.plan.finish_iteration(result, 'done')
            except life.CheckpointError as exc:
                errors.append(str(exc))
                # Do not persist an in-memory quote result lost before checkpoint.
                if scenario not in {'interrupted_quote', 'composite', 'unknown'}:
                    world.plan.finish_iteration(None, 'stopped')
        await world.close()
        if scenario in {'unknown', 'interrupted_quote'}:
            before = copy.deepcopy(world.sends)
            world.hidden = None
            strategy = await world.open(root, mp)
            try:
                await strategy._atomic_deposit_iteration(life.AMOUNT)
            except life.CheckpointError as exc:
                errors.append(str(exc))
            else:
                raise AssertionError('expected driver refusal on reopening')
            assert world.sends == before, 'reopening must not send again'
            await world.close()
    shutil.copyfile(root / 'sdk.sqlite', root / 'sdk-journal.sqlite')
    shutil.copyfile(root / 'plan.json', root / 'run-plan.json')
    return world, errors
