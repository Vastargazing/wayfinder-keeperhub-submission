"""Open the operator screen on a run's state directory.

    python -m operator_console --state-dir moonwell/state/blocked
    python -m operator_console --state-dir DIR --manifest DIR/run-manifest.json
    python -m operator_console --state-dir DIR --render screen.html
    python -m operator_console --make-fixture /tmp/fixture-run

With no ``--state-dir`` the known layouts this repository's own scripts write
(``moonwell/state/*``, ``integration/state/*``) are probed and reported. They are
runtime state and gitignored, so an empty answer in a clean checkout is the
correct answer and is printed as one rather than replaced with sample data.

Nothing here sends, signs or resolves. Serving and probing read existing state.
``--make-fixture`` creates a new state directory of synthetic data, refusing any
path that already exists. ``--render`` writes the page to the file you name;
choose an output file outside the state directory to preserve its files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .fixture import FixtureTargetError, build_fixture
from .server import ConsoleConfig, make_server, render_run
from .sources import KNOWN_STATE_ROOTS, probe_known_layouts

REPO_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="operator_console",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--state-dir", help="directory holding sdk-journal.sqlite")
    parser.add_argument("--manifest", help="a run manifest naming this state directory")
    parser.add_argument("--render", help="write the page to this file and exit; binds no socket")
    parser.add_argument("--host", default="127.0.0.1", help="canonical IPv4 loopback in 127.0.0.0/8, or localhost (no DNS); no IPv6")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--make-fixture",
                        help="create a labelled fixture state directory at a new path and exit")
    args = parser.parse_args(argv)

    if args.make_fixture:
        try:
            directory = build_fixture(Path(args.make_fixture))
        except FixtureTargetError as refusal:
            # An occupied path is an answer, not a crash: say what is there and
            # stop. Anything else this builder raises keeps its traceback.
            print(refusal, file=sys.stderr)
            return 2
        print(f"fixture state directory: {directory}")
        print("It is marked FIXTURE.txt and the screen says so before anything else.")
        print(f"  python -m operator_console --state-dir {directory} "
              f"--manifest {directory / 'run-manifest.json'}")
        return 0

    state_dir = Path(args.state_dir) if args.state_dir else None
    if state_dir is None:
        found = probe_known_layouts(REPO_ROOT)
        print("No --state-dir given. Probed the layouts this repository's scripts write:")
        for rel in KNOWN_STATE_ROOTS:
            print(f"  {REPO_ROOT / rel}")
        if not found:
            print("None of them holds an sdk-journal.sqlite. Runtime state is gitignored,")
            print("so a clean checkout has none. Pass --state-dir, or build a labelled")
            print("fixture with --make-fixture DIR.")
            return 2
        for candidate in found:
            print(f"  found: {candidate}")
        print("Pass one of them with --state-dir.")
        return 2

    config = ConsoleConfig(
        state_dir=state_dir,
        manifest=Path(args.manifest) if args.manifest else _default_manifest(state_dir),
    )

    if args.render:
        Path(args.render).write_text(render_run(config))
        print(f"wrote {args.render}")
        return 0

    try:
        server = make_server(config, args.host, args.port)
    except ValueError as refusal:
        print(refusal, file=sys.stderr)
        return 2
    host, port = server.server_address[:2]
    print(f"operator screen (reads only) on http://{host}:{port}/")
    print(f"  state directory: {state_dir}")
    print("  no write, send or retry endpoint exists; other HTTP methods answer 405")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _default_manifest(state_dir: Path) -> Path | None:
    candidate = Path(state_dir) / "run-manifest.json"
    return candidate if candidate.is_file() else None


if __name__ == "__main__":
    sys.exit(main())
