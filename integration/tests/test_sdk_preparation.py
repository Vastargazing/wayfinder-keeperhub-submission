"""The pinned recipe must be applied to the directory it names, wherever that directory sits.

Real Git and the real scripts throughout: every patch is applied by `git apply` in a child process and every
assertion is about bytes on disk or a process exit code. Nothing stands in for a successful `git apply`.

The recipe used by the placement cases is authored here by Git itself, in a disposable repository, and reaches the
shared helper through `check_sdk_snapshot.ROOT`; the pinned patches, their expected metadata and the pinned SDK
commit are never written to and never needed. That commit is an external prerequisite of the real preparation, so
depending on it here would mean skipping wherever it is absent; the end-to-end run against it belongs to
`check_sdk_snapshot.py` and to the reproduction evidence, not to this suite.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'integration' / 'scripts'))
import check_sdk_snapshot

# Resolved inside each test, not at import time: a tree without the shared, discovery-localised application of the
# recipe must fail every behavioural case by name rather than skip the whole module at collection.

PATCHES = ['01-seam.patch', '02-layer.patch', '03-plain.patch']
GIT = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0'}


def git(cwd, *args, env=None):
    """Inspection and setup Git, deliberately free of any inherited GIT_* variable a case may have set."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, text=True, capture_output=True,
                          env={**clean, **GIT, **(env or {})}).stdout


def write(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def contents(root, skip=()):
    return {str(q.relative_to(root)): hashlib.sha256(q.read_bytes()).hexdigest()
            for q in sorted(root.rglob('*')) if (q.is_file() or q.is_symlink())
            and not set(q.relative_to(root).parts) & set(skip)}


def repository(path):
    """State a preparation run must not change: the commit, the staged content and the working tree."""
    return dict(head=git(path, 'rev-parse', 'HEAD').strip(), stage=git(path, 'ls-files', '-s'),
                status=git(path, 'status', '--porcelain=v1'), files=contents(path, skip={'.git'}))


@pytest.fixture(scope='module')
def recipe(tmp_path_factory):
    """Three patches in the shipped shape: two git-style diffs, one of which creates files, then a plain one."""
    home = tmp_path_factory.mktemp('recipe')
    author = home / 'author'
    author.mkdir()
    git(author.parent, 'init', '--quiet', '-b', 'main', str(author))
    base = {'pkg/core/alpha.py': 'ALPHA = 1\nKEEP = "unchanged"\n', 'pkg/adapters/beta.py': 'BETA = 1\n'}
    write(author, base)
    git(author, 'add', '-A')
    git(author, '-c', 'user.name=recipe', '-c', 'user.email=recipe@example.invalid', 'commit', '--quiet', '-m', 'base')
    steps = [
        {'pkg/core/alpha.py': 'ALPHA = 2\nKEEP = "unchanged"\n',
         'pkg/core/created.py': 'CREATED_BY_THE_FIRST_PATCH = True\n'},
        {'pkg/adapters/beta.py': 'BETA = 2\n'},
        {'pkg/core/alpha.py': 'ALPHA = 3\nKEEP = "unchanged"\n'},
    ]
    project = home / 'project'
    (project / 'patches').mkdir(parents=True)
    for name, step in zip(PATCHES, steps):
        write(author, step)
        git(author, 'add', '-N', '.')
        diff = git(author, 'diff')
        if name.endswith('plain.patch'):
            # A traditional unified diff, like the third shipped patch: no `diff --git` or `index` header.
            diff = ''.join(l + '\n' for l in diff.splitlines() if not l.startswith(('diff --git ', 'index ')))
        (project / 'patches' / name).write_text(diff)
        git(author, 'add', '-A')
        git(author, '-c', 'user.name=recipe', '-c', 'user.email=recipe@example.invalid', 'commit', '--quiet', '-m', name)
    expected = {rel: (author / rel).read_text() for rel in
                ['pkg/core/alpha.py', 'pkg/core/created.py', 'pkg/adapters/beta.py']}
    assert expected['pkg/core/alpha.py'] == 'ALPHA = 3\nKEEP = "unchanged"\n'
    return dict(project=project, base=base, expected=expected)


def place(tmp_path, recipe, kind):
    """Build one placement of the SDK directory and return it with the surrounding repository, if any."""
    if kind == 'plain-directory':
        sdk = tmp_path / 'extracted' / 'wayfinder' / 'upstream'
        sdk.mkdir(parents=True)
        return sdk, None
    outer = tmp_path / ('a path with spaces' if 'space' in kind else 'clone')
    outer.mkdir(parents=True)
    git(outer.parent, 'init', '--quiet', '-b', 'main', str(outer))
    write(outer, {'README.md': 'the surrounding project\n'})
    git(outer, 'add', '-A')
    git(outer, '-c', 'user.name=outer', '-c', 'user.email=outer@example.invalid', 'commit', '--quiet', '-m', 'outer')
    sdk = outer / 'wayfinder' / 'upstream'
    sdk.mkdir(parents=True)
    return sdk, outer


def decoy(tmp_path, name):
    """A disposable repository, used only as the target of an inherited Git variable."""
    d = tmp_path / name
    d.mkdir(parents=True)
    git(d.parent, 'init', '--quiet', '-b', 'main', str(d))
    write(d, {'kept.txt': 'this repository must not be touched\n'})
    git(d, 'add', '-A')
    git(d, '-c', 'user.name=decoy', '-c', 'user.email=decoy@example.invalid', 'commit', '--quiet', '-m', 'decoy')
    return d


PLACEMENTS = ['plain-directory', 'inside-clone', 'inside-clone-with-spaces',
              'inherited-GIT_DIR', 'inherited-GIT_WORK_TREE', 'inherited-GIT_INDEX_FILE',
              'inherited-GIT_CEILING_DIRECTORIES']


@pytest.mark.parametrize('kind', PLACEMENTS)
def test_recipe_is_applied_to_the_named_directory(tmp_path, monkeypatch, recipe, kind):
    monkeypatch.setattr(check_sdk_snapshot, 'ROOT', recipe['project'])
    sdk, outer = place(tmp_path, recipe, kind)
    write(sdk, recipe['base'])
    target = None
    if kind.startswith('inherited-'):
        variable = kind.split('-', 1)[1]
        target = decoy(tmp_path, 'decoy')
        monkeypatch.setenv(variable, {'GIT_DIR': str(target / '.git'), 'GIT_WORK_TREE': str(target),
                                      'GIT_INDEX_FILE': str(target / '.git' / 'index'),
                                      'GIT_CEILING_DIRECTORIES': str(tmp_path)}[variable])
    before = repository(outer) if outer else None
    kept = contents(target) if target else None
    out = tmp_path / 'recipe-output'
    out.mkdir()

    rows = check_sdk_snapshot.apply_recipe(sdk, out, patches=PATCHES)

    assert rows[0]['name'] == 'git-discovery' and rows[0]['exit'] != 0
    assert [row['exit'] for row in rows[1:]] == [0] * 6
    assert {rel: (sdk / rel).read_text() for rel in recipe['expected']} == recipe['expected']
    assert contents(sdk) == {rel: hashlib.sha256(text.encode()).hexdigest()
                             for rel, text in recipe['expected'].items()}
    assert not (sdk / '.git').exists()
    if outer:
        assert repository(outer)['stage'] == before['stage'] and repository(outer)['head'] == before['head']
        assert 'README.md' not in repository(outer)['status']
    if target:
        assert contents(target) == kept


def test_every_placement_reconstructs_the_same_source(tmp_path, monkeypatch, recipe):
    monkeypatch.setattr(check_sdk_snapshot, 'ROOT', recipe['project'])
    produced = []
    for index, kind in enumerate(['plain-directory', 'inside-clone', 'inside-clone-with-spaces']):
        room = tmp_path / str(index)
        room.mkdir()
        sdk, _ = place(room, recipe, kind)
        write(sdk, recipe['base'])
        out = room / 'recipe-output'
        out.mkdir()
        check_sdk_snapshot.apply_recipe(sdk, out, patches=PATCHES)
        produced.append(contents(sdk))
    assert produced[1] == produced[0] and produced[2] == produced[0]


def test_a_git_style_patch_is_ignored_when_a_repository_is_discovered(tmp_path, recipe):
    """Why the fix exists, stated in terms of Git alone: this holds on any version of these scripts."""
    sdk, outer = place(tmp_path, recipe, 'inside-clone')
    write(sdk, recipe['base'])
    patch = recipe['project'] / 'patches' / PATCHES[0]
    inherited = subprocess.run(['git', 'apply', str(patch)], cwd=sdk, text=True, capture_output=True,
                               env={**os.environ, **GIT})
    assert inherited.returncode == 0 and inherited.stderr == ''
    assert (sdk / 'pkg/core/alpha.py').read_text() == recipe['base']['pkg/core/alpha.py']
    assert not (sdk / 'pkg/core/created.py').exists()


def test_the_patch_environment_localises_that_discovery(tmp_path, recipe):
    """The same command and the same configuration isolation; only the discovery localisation differs."""
    sdk, outer = place(tmp_path, recipe, 'inside-clone')
    write(sdk, recipe['base'])
    patch = recipe['project'] / 'patches' / PATCHES[0]
    localised = subprocess.run(['git', 'apply', str(patch)], cwd=sdk, text=True, capture_output=True,
                               env={**check_sdk_snapshot.sdk_git_env(sdk), **GIT})
    # Git may warn about file modes here; the reconstruction is judged by the bytes it produced.
    assert localised.returncode == 0
    assert (sdk / 'pkg/core/created.py').read_text() == recipe['expected']['pkg/core/created.py']
    assert 'README.md' not in repository(outer)['status']


def test_a_destination_that_is_itself_a_repository_is_refused(tmp_path, monkeypatch, recipe):
    monkeypatch.setattr(check_sdk_snapshot, 'ROOT', recipe['project'])
    sdk = tmp_path / 'sdk'
    sdk.mkdir()
    git(tmp_path, 'init', '--quiet', '-b', 'main', str(sdk))
    write(sdk, recipe['base'])
    out = tmp_path / 'recipe-output'
    out.mkdir()
    before = contents(sdk / 'pkg')
    with pytest.raises(ValueError, match='still resolves into a Git repository'):
        check_sdk_snapshot.apply_recipe(sdk, out, patches=PATCHES)
    assert contents(sdk / 'pkg') == before
    assert (out / 'git-discovery.log').exists()


@pytest.mark.parametrize('case', ['existing-destination', 'existing-output', 'pinned-object-missing'])
def test_preparation_refusals_are_preserved(tmp_path, case):
    """The real CLI, unchanged refusals; none of these reaches the network."""
    dest = tmp_path / 'sdk'
    out = tmp_path / 'recipe'
    objects = tmp_path / 'objects'
    objects.mkdir()
    git(tmp_path, 'init', '--quiet', '-b', 'main', str(objects))
    write(objects, {'unrelated.txt': 'not the SDK\n'})
    git(objects, 'add', '-A')
    git(objects, '-c', 'user.name=o', '-c', 'user.email=o@example.invalid', 'commit', '--quiet', '-m', 'o')
    if case == 'existing-destination':
        dest.mkdir()
    if case == 'existing-output':
        out.mkdir()
    done = subprocess.run([sys.executable, str(ROOT / 'integration/scripts/prepare_sdk_snapshot.py'),
                           '--objects', str(objects), '--dest', str(dest), '--out', str(out)],
                          cwd=tmp_path, text=True, capture_output=True, env={**os.environ, **GIT})
    assert done.returncode != 0
    if case == 'existing-destination':
        assert 'destination must not exist' in done.stderr
    if case == 'pinned-object-missing':
        assert check_sdk_snapshot.SDK_BASE in done.stderr and 'archive' in done.stderr
        assert not (dest / 'wayfinder_paths').exists()


def test_both_consumers_share_one_recipe_application():
    """The preparation script and the checker must not grow a second copy of the patch loop."""
    prepare = (ROOT / 'integration/scripts/prepare_sdk_snapshot.py').read_text()
    checker = (ROOT / 'integration/scripts/check_sdk_snapshot.py').read_text()
    assert prepare.count('apply_recipe(') == 1 and "'git','apply'" not in prepare
    assert checker.count("'git','apply'") == 2 and checker.count('def apply_recipe(') == 1
    assert checker.index("'git','apply'") > checker.index('def apply_recipe(')
    # Preparation stays offline: neither script has a way to reach another copy of the SDK.
    for source in (prepare, checker):
        assert "'fetch'" not in source and "'clone'" not in source
        assert 'http://' not in source and 'https://' not in source
