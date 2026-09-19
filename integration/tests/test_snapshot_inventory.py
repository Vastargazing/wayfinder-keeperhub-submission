"""The shipped inventory verifier through its real CLI.

A normal Git clone's own root `.git` directory is the only path the verifier skips.
Everything else that is not listed in SNAPSHOT-MANIFEST.json stays refused: modified or
missing listed files, extra and hidden files, symlinks, `.git` directories anywhere else,
and a root `.git` that is a symlink or a file. Every case runs the actual script as a
subprocess on its own disposable synthetic tree and asserts the process exit code against
the parsed JSON. No git command is executed: the root `.git` is a hand-made directory of
typical metadata file names, which is all the verifier may look at.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERIFIER = ROOT / 'integration' / 'scripts' / 'verify_snapshot.py'
SDK_FILE = b'sdk\n'
GIT_METADATA = {
    'HEAD': b'ref: refs/heads/main\n',
    'config': b'[core]\n\trepositoryformatversion = 0\n',
    'description': b'Unnamed repository\n',
    'index': b'DIRC\x00\x00\x00\x02',
    'packed-refs': b'# pack-refs with: peeled\n',
    'info/exclude': b'# exclude\n',
    'refs/heads/main': b'0' * 40 + b'\n',
    'objects/ab/' + 'c' * 38: b'\x78\x01',
    'hooks/pre-commit.sample': b'#!/bin/sh\n',
    'logs/HEAD': b'log\n',
}
# In-process observer: records every path the verifier opens or lists, then exits with the verifier's own code.
OBSERVER = r'''
import json, runpy, sys
script, record, *argv = sys.argv[1:]
touched = []
def hook(event, args):
    if event in ('open', 'os.scandir', 'os.listdir') and args and args[0] is not None:
        touched.append(str(args[0]))
sys.addaudithook(hook)
sys.argv = [script, *argv]
code = 1
try:
    runpy.run_path(script, run_name='__main__')
    code = 0
except SystemExit as stop:
    code = int(stop.code) if isinstance(stop.code, int) else 1
finally:
    with open(record, 'w') as handle:
        json.dump(touched, handle)
sys.exit(code)
'''


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def make_tree(root):
    files = {
        'README.md': b'# synthetic snapshot\n',
        '.gitignore': b'*.egg-info/\n',
        'docs/reproduction.md': b'clone or archive\n',
        'console/pkg/__init__.py': b'VALUE = 1\n',
        'patches/reconstruction.json': json.dumps({'wayfinder': {'base': '0' * 40, 'files': {'wayfinder_paths/__init__.py': sha256(SDK_FILE)}}}).encode(),
        'integration/scripts/verify_snapshot.py': VERIFIER.read_bytes(),
    }
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {'scope': 'synthetic test tree', 'files': {rel: sha256(data) for rel, data in files.items()}}
    (root / 'SNAPSHOT-MANIFEST.json').write_text(json.dumps(manifest, indent=1))
    return root


def add_git_metadata(parent, name='.git'):
    for rel, data in GIT_METADATA.items():
        path = parent / name / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def symlink(target, link):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f'symlinks unavailable here: {error}')


def verify(root, *args, observe=None):
    script = root / 'integration' / 'scripts' / 'verify_snapshot.py'
    if observe is None:
        command = [sys.executable, str(script), *args]
    else:
        command = [sys.executable, '-c', OBSERVER, str(script), str(observe), *args]
    done = subprocess.run(command, cwd=root, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=120)
    assert done.stderr == '', done.stderr  # refusals are reported in the JSON, never as a traceback
    report = json.loads(done.stdout)
    assert done.returncode == int(bool(report['errors'])), (done.returncode, report['errors'])  # the child's exit is the verdict
    return done.returncode, report


def normalised(errors):
    return sorted((name, kind.split(';')[0]) for name, kind in errors)


def _sdk(root, data=SDK_FILE):
    path = root / 'wayfinder' / 'upstream' / 'wayfinder_paths' / '__init__.py'
    path.parent.mkdir(parents=True)
    path.write_bytes(data)


def _clone(root, outside):
    add_git_metadata(root)


def _modified(root, outside):
    add_git_metadata(root)
    (root / 'README.md').write_bytes(b'# changed\n')


def _missing(root, outside):
    add_git_metadata(root)
    (root / 'docs' / 'reproduction.md').unlink()


def _extra(root, outside):
    add_git_metadata(root)
    (root / 'console' / 'extra.py').write_bytes(b'x = 2\n')


def _hidden(root, outside):
    add_git_metadata(root)
    (root / '.hidden').write_bytes(b'h\n')
    (root / 'docs' / '.secret').write_bytes(b's\n')


def _listed_replaced_by_symlink(root, outside):
    add_git_metadata(root)
    (outside / 'README.md').write_bytes((root / 'README.md').read_bytes())
    (root / 'README.md').unlink()
    symlink(outside / 'README.md', root / 'README.md')


def _extra_symlinks(root, outside):
    add_git_metadata(root)
    (outside / 'file.py').write_bytes(b'x\n')
    (outside / 'dir').mkdir()
    (outside / 'dir' / 'inner.txt').write_bytes(b'inner\n')
    symlink(outside / 'file.py', root / 'console' / 'link.py')
    symlink(outside / 'dir', root / 'docs' / 'linked')


def _nested_git(root, outside):
    add_git_metadata(root)
    (root / 'console' / '.git').mkdir()
    (root / 'console' / '.git' / 'config').write_bytes(b'[core]\n')


def _egg_info(root, outside):
    (root / 'console' / 'pkg.egg-info').mkdir()
    (root / 'console' / 'pkg.egg-info' / 'PKG-INFO').write_bytes(b'Name: pkg\n')


def _sdk_without_flag(root, outside):
    _sdk(root)


def _sdk_with_flag(root, outside):
    add_git_metadata(root)
    _sdk(root)


def _sdk_recipe_mismatch(root, outside):
    add_git_metadata(root)
    _sdk(root, b'other\n')


def _gitfile(root, outside):
    (root / '.git').write_bytes(b'gitdir: ../elsewhere/.git\n')


# name: (setup, arguments, expected errors, root .git directory skipped, expected generated extras)
CASES = {
    'archive-clean': (lambda root, outside: None, (), [], False, []),
    'clone-clean': (_clone, (), [], True, []),
    'clone-modified-listed': (_modified, (), [('README.md', 'hash mismatch')], True, []),
    'clone-missing-listed': (_missing, (), [('docs/reproduction.md', 'missing or symlink')], True, []),
    'clone-extra-file': (_extra, (), [('console/extra.py', 'unlisted file')], True, []),
    'clone-hidden-extras': (_hidden, (), [('.hidden', 'unlisted file'), ('docs/.secret', 'unlisted file')], True, []),
    'clone-listed-replaced-by-symlink': (_listed_replaced_by_symlink, (), [('README.md', 'missing or symlink')], True, []),
    'clone-extra-symlinks-file-and-dir': (_extra_symlinks, (), [('console/link.py', 'unlisted file'), ('docs/linked', 'unlisted file')], True, []),
    'clone-nested-git-is-not-root-metadata': (_nested_git, (), [('console/.git/config', 'unlisted file')], True, []),
    'archive-egg-info-tolerated': (_egg_info, (), [], False, ['console/pkg.egg-info/PKG-INFO']),
    'archive-prepared-sdk-without-flag': (_sdk_without_flag, (), [('wayfinder/upstream/wayfinder_paths/__init__.py', 'unlisted file')], False, []),
    'clone-prepared-sdk-with-flag': (_sdk_with_flag, ('--prepared-sdk',), [], True, ['wayfinder/upstream/wayfinder_paths/__init__.py']),
    'clone-prepared-sdk-recipe-mismatch': (_sdk_recipe_mismatch, ('--prepared-sdk',), [('wayfinder/upstream/wayfinder_paths/__init__.py', 'prepared recipe mismatch')], True, []),
    'root-gitfile-unsupported': (_gitfile, (), [('.git', 'root .git is not a directory')], False, []),
}


@pytest.mark.parametrize('name', sorted(CASES))
def test_inventory_verdicts(tmp_path, name):
    setup, arguments, expected_errors, expected_skipped, expected_extras = CASES[name]
    root = make_tree(tmp_path / 'tree')
    outside = tmp_path / 'outside'
    outside.mkdir()
    setup(root, outside)
    code, report = verify(root, *arguments)
    assert normalised(report['errors']) == sorted(expected_errors), report
    assert code == (1 if expected_errors else 0)
    assert bool(report.get('root_git_directory_skipped')) is expected_skipped
    for extra in expected_extras:
        assert extra in report['non_distributed_generated_files']
    # Clone metadata is never presented as generated or distributed content.
    assert not [n for n in report['non_distributed_generated_files'] if n == '.git' or n.startswith('.git/')]


def test_root_git_symlink_is_refused_without_reading_its_target(tmp_path):
    root = make_tree(tmp_path / 'tree')
    add_git_metadata(tmp_path, name='canary')
    canary = tmp_path / 'canary'
    symlink(canary, root / '.git')
    record = tmp_path / 'touched.json'
    code, report = verify(root, observe=record)
    assert code == 1 and normalised(report['errors']) == [('.git', 'root .git is a symlink')]
    assert bool(report.get('root_git_directory_skipped')) is False
    touched = json.loads(record.read_text())
    assert touched, 'observer recorded nothing'
    assert not [t for t in touched if t.startswith((str(canary), str(canary.resolve())))], touched
    assert 'canary' not in json.dumps(report)


def test_symlinks_inside_root_git_metadata_are_not_followed(tmp_path):
    root = make_tree(tmp_path / 'tree')
    add_git_metadata(root)
    canary = tmp_path / 'canary'
    canary.mkdir()
    (canary / 'secret.txt').write_bytes(b'do not read\n')
    symlink(canary, root / '.git' / 'hooks' / 'escape')
    symlink(canary / 'secret.txt', root / '.git' / 'note')
    record = tmp_path / 'touched.json'
    code, report = verify(root, observe=record)
    assert (code, report['errors'], report.get('root_git_directory_skipped')) == (0, [], True)
    touched = json.loads(record.read_text())
    assert touched, 'observer recorded nothing'
    inside = str(root / '.git')
    assert not [t for t in touched if t.startswith((str(canary), str(canary.resolve()), inside + os.sep)) or t == inside], touched
    assert 'canary' not in json.dumps(report)


def test_verification_does_not_modify_the_clone(tmp_path):
    root = make_tree(tmp_path / 'tree')
    add_git_metadata(root)

    def snapshot():
        rows = {}
        for base, dirs, names in os.walk(root):
            for name in dirs + names:
                path = Path(base) / name
                stat = path.lstat()
                regular = path.is_file() and not path.is_symlink()
                # Directory st_size is filesystem-defined and may vary between calls (seen on FUSE); compare it for regular files only.
                rows[str(path.relative_to(root))] = (stat.st_mode, stat.st_size if regular else None, stat.st_mtime_ns, sha256(path.read_bytes()) if regular else None)
        return rows

    before = snapshot()
    code, report = verify(root)
    assert (code, report['errors']) == (0, [])
    assert snapshot() == before
