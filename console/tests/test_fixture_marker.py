"""Damaged fixture metadata remains visible through reader, CLI and real HTTP."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from operator_console.fixture import build_fixture
from operator_console.server import ConsoleConfig, ConsoleHandler, render_run
from operator_console.sources import load_state_directory
from test_console_readonly import _request


def fingerprint(path):
    if path.is_symlink(): return ['symlink', str(path.readlink())]
    if path.is_dir(): return ['directory']
    if not path.exists(): return ['absent']
    return [hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns]


@pytest.mark.parametrize('kind', ['valid', 'absent', 'invalid', 'empty', 'oserror', 'directory', 'broken-link'])
def test_marker_reader_cli_http(tmp_path, kind):
    state = build_fixture(tmp_path / 'fixture')
    marker = state / 'FIXTURE.txt'
    if kind == 'invalid': marker.write_bytes(b'\xff\xfe\x00')
    elif kind == 'empty': marker.write_bytes(b'')
    elif kind in {'absent', 'directory', 'broken-link'}:
        marker.unlink()
        if kind == 'directory': marker.mkdir()
        elif kind == 'broken-link': marker.symlink_to(state / 'missing-marker')
    protected = [state / 'sdk-journal.sqlite', state / 'run-plan.json', marker]
    before = {p.name: fingerprint(p) for p in protected}
    sidecars = {p.name: fingerprint(p) for p in state.glob('*-wal')} | {p.name: fingerprint(p) for p in state.glob('*-shm')}
    real = Path.read_text
    reads = []
    def read(path, *args, **kwargs):
        if path == marker:
            reads.append(str(path))
            if kind == 'oserror': raise OSError('synthetic marker read failure')
        return real(path, *args, **kwargs)
    damaged = kind not in {'valid', 'absent'}
    with patch.object(Path, 'read_text', read):
        source = load_state_directory(state)
        page = render_run(ConsoleConfig(state))
        handler = type('MarkerHandler', (ConsoleHandler,), {'config': ConsoleConfig(state)})
        status, body, _ = _request(handler, 'GET')
        assert status == 200
        assert ('FIXTURE DATA' in page) == (kind != 'absent')
        assert bool(source.fixture_notice) == (kind != 'absent')
        if damaged:
            assert any('FIXTURE.txt' in problem for problem in source.problems)
            for text in (page, body.decode()):
                assert 'Read problems' in text and 'may contain synthetic' in text
        assert b'Retry send' not in body and b'<form' not in body
        assert _request(handler, 'POST')[0] == 405
    # Actual process entrypoint; only the expected filesystem error is injected.
    output = tmp_path / 'page.html'
    args = ['--state-dir', str(state), '--render', str(output)]
    if kind == 'oserror':
        code = '''from pathlib import Path
import sys
from operator_console.__main__ import main
real = Path.read_text
def read(path, *a, **kw):
    if path.name == 'FIXTURE.txt': raise OSError('synthetic marker read failure')
    return real(path, *a, **kw)
Path.read_text = read
sys.exit(main(sys.argv[1:]))
'''
        command = [sys.executable, '-c', code, *args]
    else: command = [sys.executable, '-m', 'operator_console', *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    (tmp_path / 'cli.json').write_text(json.dumps(dict(exit=result.returncode, stdout=result.stdout, stderr=result.stderr)))
    assert result.returncode == 0, result.stderr
    html = output.read_text()
    assert ('FIXTURE DATA' in html) == (kind != 'absent')
    if damaged: assert 'may contain synthetic' in html and 'Read problems' in html
    assert {p.name: fingerprint(p) for p in protected} == before
    (tmp_path / 'preservation.json').write_text(json.dumps(dict(main_checkpoint_marker=before, sidecars_before=sidecars, sidecars_after={p.name:fingerprint(p) for p in state.glob('*') if p.name.endswith(('-wal','-shm'))})))
    if kind in {'valid','oserror'}: assert reads
