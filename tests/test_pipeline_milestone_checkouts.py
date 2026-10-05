import shutil
import subprocess
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from archon_horizon.pipeline.worker.lean_build import LeanBuildPolicy
from archon_horizon.pipeline.worker.milestone_jobs import checkout, execute
from archon_horizon.pipeline.worker.milestone_verify import clean_commit
from test_pipeline_milestones import sources


def command(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def repository(root, files):
    root.mkdir()
    command(root, 'init', '-q')
    command(root, 'config', 'user.name', 'Test')
    command(root, 'config', 'user.email', 'test@example.invalid')
    command(root, 'commit', '--allow-empty', '-qm', 'Base')
    base = command(root, 'rev-parse', 'HEAD')
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    command(root, 'add', '.')
    command(root, 'commit', '-qm', 'Source')
    return base, command(root, 'rev-parse', 'HEAD')


@pytest.mark.skipif(not shutil.which('lake'), reason='Lean toolchain unavailable')
def test_trusted_verification_builds_repository_without_lake_gitignore(tmp_path):
    root = tmp_path / 'roadmap'
    base, commit = repository(root, sources())
    policy = LeanBuildPolicy(root=tmp_path / 'build', minimum_free_bytes=0, timeout_seconds=90)
    daemon = SimpleNamespace(harnesses={'build': SimpleNamespace(lean_build=policy)},
        workspace_roots=(tmp_path,), publication_remotes={}, _publication_headers={},
        transport=SimpleNamespace(_post=lambda *args: SimpleNamespace(raise_for_status=lambda: None)),
        _progress=lambda *args, **kwargs: None)
    job = {'id': str(uuid4()), 'claim_token': str(uuid4()),
        'workspace': {'path': str(root), 'repository_id': str(uuid4())},
        'solution_workspace': None,
        'request': {'source_commit_oid': commit, 'base_commit_oid': base}}

    report = execute(daemon, job, threading.Event())

    assert report['kind'] == 'contract'
    assert report['source_commit_oid'] == commit
    assert report['targets']['Example.result'] == ['sorryAx']
    assert report['definitions']['Example.object'] == []
    assert not (root / '.gitignore').exists()
    assert not (root / '.lake').exists()
    assert command(root, 'status', '--porcelain') == ''
    assert not list(policy.root.glob('milestone-job-*'))


@pytest.mark.parametrize('changed', [
    'lake-manifest.json', '.lake/tracked-input.txt',
    'milestones/Unexpected.lean', 'milestones/.lake/Unexpected.lean',
])
def test_generated_output_exclusion_preserves_source_cleanliness(tmp_path, changed):
    source = tmp_path / 'source'
    _, commit = repository(source, {'lake-manifest.json': '{}\n', '.lake/tracked-input.txt': 'pinned\n'})
    root = tmp_path / 'verification'
    checkout({'path': str(source)}, commit, root, (tmp_path,), {}, {})
    output = root / '.lake' / 'build' / 'generated.olean'
    output.parent.mkdir(parents=True)
    output.write_bytes(b'generated')
    assert clean_commit(root) == commit

    path = root / changed
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('changed\n')
    with pytest.raises(ValueError, match='clean committed checkout'):
        clean_commit(root)
