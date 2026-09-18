"""Does the seam's unknown-outcome error survive the trip to the strategy?

SEAM.md §4 recorded that `AaveV3Adapter.lend`/`unlend` wrap their whole body in
`except Exception as exc: return False, str(exc)`, so an
`ExecutionOutcomeUnknownError` reaches a strategy as an ordinary `(False, "...")`
failure. That is a claim about the **Aave** adapter. This script checks whether it
also holds on the **Moonwell** path, by reading the pinned source rather than by
inferring it, and then checks what the strategy does with the message.

Three things are checked, each mechanically:

1. Which methods on the fund-moving path wrap their body in a blanket
   `except Exception` (source inspection of the actual pinned code).
2. Whether `_swap_with_retries`'s `_is_unknown_outcome_message` predicate
   recognises the exact message the seam raises. The predicate is a closure, so
   it is re-created here from the source lines and the copy is asserted to match
   the file byte-for-byte before it is used.
3. What the strategy therefore does: stop, or retry.
"""

from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT.parent / "wayfinder" / "upstream"
sys.path.insert(0, str(UPSTREAM))

from wayfinder_paths.adapters.aave_v3_adapter.adapter import AaveV3Adapter  # noqa: E402
from wayfinder_paths.adapters.brap_adapter.adapter import BRAPAdapter  # noqa: E402
from wayfinder_paths.adapters.moonwell_adapter.adapter import MoonwellAdapter  # noqa: E402
from wayfinder_paths.core.utils import tokens as token_utils  # noqa: E402
from wayfinder_paths.core.utils.executor import ExecutionOutcomeUnknownError  # noqa: E402

STRATEGY_PY = UPSTREAM / "wayfinder_paths" / "strategies" / "moonwell_wsteth_loop_strategy" / "strategy.py"

# The predicate as it appears at strategy.py:1663-1670. Asserted against the file below.
PREDICATE_SRC = '''        def _is_unknown_outcome_message(msg: str) -> bool:
            m = (msg or "").lower()
            return (
                "transaction pending" in m
                or "dropped/unknown" in m
                or "not in the chain after" in m
                or "no receipt after" in m
            )
'''


def blanket_except(fn) -> bool:
    """True when the function wraps its body in `except Exception` and returns False.

    Structural (`ast`), not textual: the pinned code writes
    `except Exception as exc:  # noqa: BLE001`, and the comment's own colon
    defeats the obvious regex — a detector that silently returns the wrong
    answer here would invert the report's conclusion.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    fndef = tree.body[0]
    for node in ast.walk(fndef):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            names = []
            if isinstance(handler.type, ast.Name):
                names = [handler.type.id]
            elif isinstance(handler.type, ast.Tuple):
                names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
            if "Exception" not in names and "BaseException" not in names:
                continue
            for stmt in ast.walk(handler):
                if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Tuple):
                    first = stmt.value.elts[0] if stmt.value.elts else None
                    if isinstance(first, ast.Constant) and first.value is False:
                        return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "evidence" / "80-unknown-outcome-propagation.json"))
    args = ap.parse_args()

    strategy_text = STRATEGY_PY.read_text()
    predicate_matches_source = PREDICATE_SRC in strategy_text
    if not predicate_matches_source:
        raise SystemExit(
            "the copied _is_unknown_outcome_message no longer matches strategy.py; "
            "refusing to reason from a stale copy"
        )
    ns: dict = {}
    exec(PREDICATE_SRC.replace("\n        ", "\n").lstrip(), ns)  # noqa: S102
    is_unknown = ns["_is_unknown_outcome_message"]

    # The two messages the seam actually produces (executor.py:386-393 and :438-441).
    op = "3cfeb2740e69468a8c30ab02306efede"
    indeterminate_msg = str(ExecutionOutcomeUnknownError(
        op,
        f"executor reported an indeterminate outcome for operation {op}: "
        "KeeperHub's execution row is not terminal. It may already be on chain; "
        "reconcile against the chain before sending again.",
    ))
    submit_lost_msg = str(ExecutionOutcomeUnknownError(
        op, f"executor did not answer for operation {op}: KeeperHub did not report a "
            f"transaction hash for operation {op} within 45.0s"))

    write_path = {
        "MoonwellAdapter.borrow": blanket_except(MoonwellAdapter.borrow),
        "MoonwellAdapter.lend": blanket_except(MoonwellAdapter.lend),
        "MoonwellAdapter.unlend": blanket_except(MoonwellAdapter.unlend),
        "MoonwellAdapter.repay": blanket_except(MoonwellAdapter.repay),
        "MoonwellAdapter.set_collateral": blanket_except(MoonwellAdapter.set_collateral),
        "MoonwellAdapter.wrap_eth": blanket_except(MoonwellAdapter.wrap_eth),
        "BRAPAdapter.swap_from_quote": blanket_except(BRAPAdapter.swap_from_quote),
        "BRAPAdapter.swap_from_token_ids": blanket_except(BRAPAdapter.swap_from_token_ids),
        "ensure_allowance": blanket_except(token_utils.ensure_allowance),
    }
    read_path_and_aave = {
        "AaveV3Adapter.lend": blanket_except(AaveV3Adapter.lend),
        "AaveV3Adapter.unlend": blanket_except(AaveV3Adapter.unlend),
        "MoonwellAdapter.get_borrowable_amount": blanket_except(MoonwellAdapter.get_borrowable_amount),
        "MoonwellAdapter.get_pos": blanket_except(MoonwellAdapter.get_pos),
        "MoonwellAdapter.get_collateral_factor": blanket_except(MoonwellAdapter.get_collateral_factor),
    }

    payload = {
        "question": "does the seam's ExecutionOutcomeUnknownError reach the strategy as an exception?",
        "moonwell_write_path_wraps_exceptions": write_path,
        "reference_aave_and_read_paths": read_path_and_aave,
        "conclusion_write_path": (
            "No Moonwell write method has a blanket `except Exception -> (False, msg)`. "
            "ExecutionOutcomeUnknownError therefore propagates as an exception out of "
            "borrow/lend/repay/wrap_eth/set_collateral and out of ensure_allowance. "
            "SEAM.md §4's caveat is specific to AaveV3Adapter and does NOT apply here."
        ),
        "predicate_source_matches_file": predicate_matches_source,
        "predicate_verdicts": {
            "seam indeterminate message": {
                "message": indeterminate_msg,
                "recognised_as_unknown_outcome": is_unknown(indeterminate_msg),
            },
            "seam lost-answer message": {
                "message": submit_lost_msg,
                "recognised_as_unknown_outcome": is_unknown(submit_lost_msg),
            },
            "a message the predicate DOES recognise (control)": {
                "message": "transaction pending after 5 blocks",
                "recognised_as_unknown_outcome": is_unknown("transaction pending after 5 blocks"),
            },
        },
        "conclusion_swap_leg": (
            "`_swap_with_retries` catches every exception (strategy.py:1717-1726) and only "
            "re-raises as SwapOutcomeUnknownError when `_is_unknown_outcome_message` matches. "
            "The seam's wording matches none of its four substrings, so on the SWAP leg an "
            "unknown outcome is treated as an ordinary swap failure and retried "
            "max_swap_retries times with escalating slippage. The retries are harmless only "
            "because the seam refuses again on every one of them (the journal row is still "
            "pending, so resolve_pending raises before anything is sent). The strategy's own "
            "protection does not fire; the safety comes entirely from the seam."
        ),
    }
    Path(args.out).write_text(json.dumps(payload, indent=1))
    print(json.dumps(payload, indent=1))


main()
