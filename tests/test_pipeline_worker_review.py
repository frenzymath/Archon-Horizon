"""Behavioral regressions found while reading every worker/client function."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from archon_horizon.pipeline.client import AgentClient, RetryDeferred
from archon_horizon.pipeline.worker import build_engine, lean_build, lean_verify, milestone_verify, verification_jobs
from archon_horizon.pipeline.worker.contracts import Operation
from archon_horizon.pipeline.worker.journal import DurableJournal, boot_identity
from archon_horizon.pipeline.worker.provider import PhysicalStopUnconfirmed, process_identity, terminate_owned_process


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def repository(root, files):
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Test')
    git(root, 'config', 'user.email', 'test@example.invalid')
    git(root, 'commit', '--allow-empty', '-qm', 'Base')
    base = git(root, 'rev-parse', 'HEAD')
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Sources')
    return base, git(root, 'rev-parse', 'HEAD')


def test_build_diagnostics_drain_stderr_larger_than_a_pipe(tmp_path):
    diagnostics = []
    code = build_engine._command([sys.executable, '-c',
        "import sys; sys.stderr.write('e'*1048576); print('finished')"], tmp_path,
        os.environ, time.monotonic() + 10, quiet=True, diagnostics=diagnostics)
    assert code == 0
    assert 'finished' in diagnostics[0]
    assert len(diagnostics[0]) < 20000


@pytest.mark.parametrize('commit_change', [False, True])
def test_library_receipt_refuses_source_changes_during_final_audit(tmp_path, monkeypatch, commit_change):
    root = tmp_path / 'library'
    base, _ = repository(root, {'lean-toolchain': 'leanprover/lean4:v4.33.1\n',
        'lake-manifest.json': '{"packages": []}\n', 'Result.lean': 'theorem result : True := True.intro\n'})
    def changed_audit(*args, **kwargs):
        (root / 'Result.lean').write_text('theorem result : False := by sorry\n')
        if commit_change:
            git(root, 'add', '.')
            git(root, 'commit', '-qm', 'Concurrent edit')
        return {}
    monkeypatch.setattr(lean_verify, 'audit', changed_audit)
    with pytest.raises(ValueError, match='changed during verification|clean committed checkout'):
        lean_verify.verify_library(root, base, lean_build.LeanBuildPolicy(root=tmp_path / 'builds'))


def test_library_verification_needs_no_milestone_planning_metadata(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    base, commit = repository(root, {'lean-toolchain': 'leanprover/lean4:v4.33.1\n',
        'lake-manifest.json': '{"packages": []}\n', 'Result.lean': 'theorem result : True := True.intro\n'})
    modules = []
    monkeypatch.setattr(lean_verify, 'audit', lambda root, locators, definitions, policy:
        modules.extend(definitions) or {'targets': {}, 'definitions': {'result': []}, 'types': {},
        'declaration_modules': {}, 'direct_admissions': []})
    report = lean_verify.verify_library(root, base, lean_build.LeanBuildPolicy(root=tmp_path / 'builds'))
    assert modules == ['Result']
    assert report['kind'] == 'library' and report['source_commit_oid'] == commit
    assert report['milestone_keys'] == [] and report['objective_paths'] == []


def test_legacy_comparator_invokes_shared_build_checker(tmp_path, monkeypatch):
    from test_pipeline_milestones import sources
    root = tmp_path / 'roadmap'
    base, _ = repository(root, sources())
    solution = tmp_path / 'solution'
    subprocess.run(['git', 'clone', '-q', str(root), str(solution)], check=True)
    git(solution, 'config', 'user.name', 'Test')
    git(solution, 'config', 'user.email', 'test@example.invalid')
    config = solution / 'comparator.json'
    config.write_text(json.dumps({'theorem_names': ['Example.endpoint', 'Example.result'],
        'permitted_axioms': ['sorryAx'], 'definition_names': ['Example.object'],
        'solution_module': 'Solution', 'challenge_module': 'Challenge'}))
    git(solution, 'add', '.')
    git(solution, 'commit', '-qm', 'Comparator configuration')
    monkeypatch.setattr(milestone_verify, 'audit', lambda *args, **kwargs:
        {'targets': {'Example.endpoint': ['sorryAx'], 'Example.result': ['sorryAx']},
         'definitions': {'Example.object': []}, 'types': {}, 'direct_admissions': [],
         'declaration_modules': {'Example.endpoint': 'Example.Endpoint', 'Example.result': 'Example.M01'}})
    invoked = []
    def unavailable_tools(root, targets, policy, **kwargs):
        invoked.extend(targets)
        return {'ok': False, 'returncode': 1}
    monkeypatch.setattr(lean_build, '_check', unavailable_tools)
    with pytest.raises(ValueError, match='Pinned comparator tools did not build'):
        milestone_verify.verify(root, base, lean_build.LeanBuildPolicy(root=tmp_path / 'builds'),
            solution_root=solution, comparator_config=config)
    assert invoked == ['@Comparator/comparator', '@lean4export/lean4export']


@pytest.mark.parametrize('leader_exits', [False, True])
def test_verification_stop_fences_separate_compiler_groups(tmp_path, leader_exits):
    child_path = tmp_path / 'child-pid'
    script = "import subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],process_group=0); " \
        "open(sys.argv[1],'w').write(str(child.pid)); time.sleep(0.3); " + ('sys.exit(0)' if leader_exits else 'time.sleep(60)')
    process = subprocess.Popen([sys.executable, '-c', script, str(child_path)], start_new_session=True)
    identity = {'pid': process.pid, 'process_identity': process_identity(process.pid), 'boot_id': boot_identity()}
    try:
        deadline = time.monotonic() + 10
        while not child_path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        child = int(child_path.read_text())
        if leader_exits:
            process.wait(timeout=10)
        verification_jobs.stop_process(process, identity)
        assert process.poll() is not None
        status = Path(f'/proc/{child}/stat')
        assert not status.exists() or status.read_text().rsplit(')', 1)[1].split()[0] in {'Z', 'X'}
    finally:
        terminate_owned_process(identity)
        process.wait(timeout=10)


def test_verifier_does_not_settle_or_reclaim_capacity_after_unconfirmed_stop(monkeypatch):
    stop = threading.Event()
    calls = []
    def post(path, body, **kwargs):
        calls.append(path)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'job': {'id': 'check', 'claim_token': 'claim'}})
    daemon = SimpleNamespace(_progress=lambda *args, **kwargs: None, host_id='host', milestone_checks=False,
        transport=SimpleNamespace(_post=post))
    def unknown_stop(*args):
        raise PhysicalStopUnconfirmed('Compiler still running')
    monkeypatch.setattr(verification_jobs, 'execute', unknown_stop)
    verification_jobs.serve(daemon, stop)
    assert stop.is_set()
    assert calls == ['/api/v3/worker/verification-jobs/claim']


def test_workspace_prepared_receipt_survives_the_offline_telemetry_horizon(tmp_path):
    journal = DurableJournal(tmp_path / 'journal', minimum_free_bytes=0, max_offline_seconds=1)
    try:
        receipt = Operation.create('execution', 1, 'workspace_prepared', {'workspace_id': 'workspace'})
        journal.enqueue(receipt)
        claimed = journal.claim(now=time.time() + 10)
        assert claimed and claimed.operation == receipt
    finally:
        journal.close()


def test_transport_outage_persists_agent_intent_backoff_across_clients(tmp_path):
    calls = []
    def outage(request):
        calls.append(request.headers['Idempotency-Key'])
        raise httpx.ReadTimeout('Lost response', request=request)
    with httpx.Client(transport=httpx.MockTransport(outage)) as transport:
        client = AgentClient('https://horizon.invalid', 'secret', 'execution', tmp_path, client=transport)
        with pytest.raises(httpx.ReadTimeout):
            client.request('POST', '/api/v3/commands', {'operation': 'test'})
        pending = client.pending()[0]
        assert pending['attempts'] == 1 and pending['retry_at'] > time.time()
        assert pending['status'] == 'pending'
        restarted = AgentClient('https://horizon.invalid', 'secret', 'execution', tmp_path, client=transport)
        with pytest.raises(RetryDeferred):
            restarted.request('POST', '/api/v3/commands', {'operation': 'test'})
        assert len(calls) == 3 and len(set(calls)) == 1
