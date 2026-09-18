"""Shared setup for the console suite.

``operator_console`` is imported from this checkout when it has not been
installed into the root ``.venv``; where it has, this is the same directory and
nothing changes. ``runbuilder`` writes the fixture run directories.
"""

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CONSOLE = HERE.parent
for entry in (str(CONSOLE), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import runbuilder  # noqa: E402


@pytest.fixture
def run_dir(tmp_path):
    """A blocked run: two landed steps, one indeterminate, one unbound step."""
    return runbuilder.write_run(
        tmp_path / "run",
        corroboration={
            runbuilder.LEND: [
                {"label": "mwstETH balance rose by the expected amount",
                 "value": "50000000000000000",
                 "source": "chain reading recorded by the run"}
            ]
        },
    )


@pytest.fixture
def state(run_dir):
    from operator_console.sources import load_state_directory

    return load_state_directory(run_dir, run_dir / "run-manifest.json")


@pytest.fixture
def view(state):
    from operator_console.model import build_view

    return build_view(state)


@pytest.fixture
def page(view):
    from operator_console.render import render_page

    return render_page(view)
