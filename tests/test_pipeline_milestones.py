import json
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from archon_horizon.pipeline import models
from archon_horizon.pipeline.projects import milestone_jobs
from archon_horizon.pipeline.auth import Actor
from archon_horizon.pipeline.projects.catalog import create_catalog
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.projects.milestone_sources import source_manifest, milestone_table
from archon_horizon.pipeline.projects.milestones import AcceptBaseline, CheckReport, CheckSubmission, accept_baseline, register_check, require_baseline, projected_manifest, review_panel, objective_view
from archon_horizon.pipeline.persistence.records import create, change, get, object_ref, snapshot
from archon_horizon.pipeline.roadmap_index import index_snapshot
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.worker.lean_build import LeanBuildPolicy
from archon_horizon.pipeline.worker.milestone_verify import verify
from archon_horizon.pipeline.worker.milestone_jobs import execute as execute_verification
from test_pipeline_service import service_database, world
from test_pipeline_reviewer_invocations import review


def markdown(metadata, body='Statement'):
    return '---\n' + json.dumps(metadata) + '\n---\n\n' + body


def sources(contract=True):
    milestone = {'id': 'M01', 'objective': 'objectives/example.md'}
    objective = {'root': 'milestones/Example'}
    result = {}
    if contract:
        milestone.update(contract={'path': 'milestones/Example/M01.lean', 'module': 'Example.M01',
                                   'declarations': ['Example.result']}, definitions=['milestones/Example/Definitions/Basic.lean'],
                         statement='accepted', references=[{'cite_key': 'source', 'locator': 'Theorem 1'}])
        objective['endpoint'] = {'path': 'milestones/Example/Endpoint.lean', 'module': 'Example.Endpoint',
                                 'declarations': ['Example.endpoint']}
        result.update({'milestones/Example/Definitions/Basic.lean': 'def Example.object : Nat := 1\n',
            'milestones/Example/M01.lean': 'import Example.Definitions.Basic\ntheorem Example.result : Example.object = 1 := by sorry\n',
            'milestones/Example/Endpoint.lean': 'import Example.Definitions.Basic\ntheorem Example.endpoint : Example.object = 1 := by sorry\n',
            'lean-toolchain': 'leanprover/lean4:v4.33.1\n',
            'lakefile.toml': 'name = "milestone_test"\n[[lean_lib]]\nname = "Example"\nsrcDir = "milestones"\n',
            'lake-manifest.json': '{"version":"1.2.0","packagesDir":".lake/packages","packages":[],"name":"milestone_test","lakeDir":".lake"}\n'})
    result['objectives/example.md'] = markdown({'title': 'Example', 'milestones': objective})
    result['nodes/example/m01.md'] = markdown({'title': 'First milestone', 'label': 'example-m01', 'milestone': milestone})
    result['milestones/Example/milestones.md'] = milestone_table(source_manifest(result), 'objectives/example.md')
    return result


def test_objective_scoped_identity_and_locators():
    manifest = source_manifest(sources())
    assert manifest['nodes']['example-m01']['milestone']['id'] == 'M01'
    invalid = sources()
    invalid['nodes/duplicate.md'] = invalid['nodes/example/m01.md'].replace('example-m01', 'duplicate')
    with pytest.raises(ValueError, match='Duplicate milestone'):
        source_manifest(invalid)
    invalid = sources()
    del invalid['milestones/Example/M01.lean']
    with pytest.raises(ValueError, match='missing'):
        source_manifest(invalid)
    invalid = sources()
    invalid['nodes/helper.md'] = markdown({'belongs_to': ['missing']})
    with pytest.raises(ValueError, match='belongs_to'):
        source_manifest(invalid)
    other = {path.replace('example', 'other').replace('Example', 'Other'):
             text.replace('example', 'other').replace('Example', 'Other') for path, text in sources().items()}
    combined = source_manifest({**sources(), **other})
    assert len(combined['objectives']) == 2
    assert [node['milestone']['id'] for node in combined['nodes'].values()] == ['M01', 'M01']


def test_proof_progress_does_not_change_contract_digest_but_definitions_do():
    original = sources()
    progress = {**original, 'nodes/example/m01.md': original['nodes/example/m01.md'].replace(
        '"statement": "accepted"', '"statement": "accepted", "proof": "in_progress"')}
    assert source_manifest(original)['digest'] == source_manifest(progress)['digest']
    progress['milestones/Example/Definitions/Basic.lean'] += 'def Example.other : Nat := 2\n'
    assert source_manifest(original)['digest'] != source_manifest(progress)['digest']


def test_helper_graph_and_checkboxes_do_not_reopen_contracts():
    original = sources()
    original['objectives/example.md'] += '\n- [ ] M01\n'
    progress = {**original, 'objectives/example.md': original['objectives/example.md'].replace('[ ]', '[x]')}
    progress['nodes/example/helper.md'] = markdown({'label': 'helper', 'belongs_to': ['example-m01']})
    progress['nodes/example/m01.md'] = progress['nodes/example/m01.md'].replace('"label":', '"children": ["helper"], "label":')
    assert source_manifest(original)['digest'] == source_manifest(progress)['digest']


def test_index_retains_lean_sources_and_same_manifest(world):
    data = sources()
    repo = world.document['source_repository_id']
    index_snapshot(world.conn, world.actor, repo, 'a' * 40, data)
    assert projected_manifest(world.conn, repo, 'a' * 40)['digest'] == source_manifest(data)['digest']
    with pytest.raises(DomainError, match='exact accepted'):
        projected_manifest(world.conn, repo, 'b' * 40)


def report(**changes):
    manifest = source_manifest(sources())
    defaults = dict(source_commit_oid='a' * 40, base_commit_oid='b' * 40, kind='contract',
        manifest_digest=manifest['digest'], base_manifest_digest='0' * 64, ancestor_commits=['a' * 40, 'b' * 40],
        milestone_keys=['example-m01'], objective_paths=['objectives/example.md'], compiled=True,
        toolchain='leanprover/lean4:v4.33.1')
    return CheckReport(**{**defaults, **changes})


def reviewed_gate(world, review, commit, number, names):
    item = create(world.conn, 'forge_item', repository_id=world.document['source_repository_id'], remote_number=number,
        kind='pull_request', review_phase='preprocessing', target_branch='main', title='Reviewed milestones',
        status='merged', head_commit_oid=commit, observed_at=datetime.now(timezone.utc))
    for i, name in enumerate(names):
        descriptor = create(world.conn, 'reviewer_descriptor', project_id=world.project['id'], slug=f'{number}-{name}',
            functions=['reviewer'], instructions=f'Inspect {name}')
        world.conn.execute(tables['review_policy_reviewer'].insert().values(review_policy_id=world.policy['id'],
            reviewer_descriptor_id=descriptor['id']))
        revision = snapshot(world.conn, 'reviewer_descriptor', descriptor, world.actor.id)
        manifest = create(world.conn, 'artifact', project_id=world.project['id'], kind='blob', content={})
        request = create(world.conn, 'provider_request', provider_thread_id=review['thread']['id'], execution_id=review['execution']['id'],
            number=number * 10 + i, reason='review', status='completed', reviewer_descriptor_id=descriptor['id'],
            reviewer_descriptor_revision_id=revision, guidance_manifest_artifact_id=manifest['id'])
        assessment = create(world.conn, 'forge_review', forge_item_id=item['id'], remote_id=str(i), reviewer_remote_id=name,
            reviewer_descriptor_id=descriptor['id'], reviewer_descriptor_revision_id=revision, provider_request_id=request['id'],
            verdict='approved', summary='Inspected exact contract and source.', commit_oid=commit, observed_at=datetime.now(timezone.utc))
        create(world.conn, 'outbox_operation', project_id=world.project['id'], actor_principal_id=world.actor.id,
            kind='forge_review', schema_version=1, idempotency_key=str(uuid4()), status='completed',
            result_ref_id=object_ref(world.conn, 'forge_review', assessment['id']), payload={
                'policy_revision_id': str(snapshot(world.conn, 'review_policy', world.policy, world.actor.id)),
                'target_branch': 'main'})
    maintainer = create(world.conn, 'forge_review', forge_item_id=item['id'], remote_id='maintainer', reviewer_remote_id='maintainer',
        verdict='approved', summary='Accept the inspected route.', commit_oid=commit, observed_at=datetime.now(timezone.utc))
    gate = create(world.conn, 'review_gate', forge_item_id=item['id'], policy_id=world.policy['id'],
        policy_revision_id=snapshot(world.conn, 'review_policy', world.policy, world.actor.id),
        maintainer_review_id=maintainer['id'], status='accepted', accepted_commit_oid=commit, evaluated_at=datetime.now(timezone.utc))
    return item, gate


@pytest.mark.parametrize('kind', ['graph', 'contract'])
def test_initial_packet_requires_receipt_for_exact_indexed_source(world, review, kind):
    change(world.conn, 'project', world.project['id'], workflow='milestones')
    repository_id = world.document['source_repository_id']
    data = sources()
    index_snapshot(world.conn, world.actor, repository_id, 'c' * 40, data)
    document = world.conn.execute(select(tables['document']).where(
        tables['document'].c.source_path == 'objectives/example.md')).mappings().one()
    workspace = create(world.conn, 'workspace', project_id=world.project['id'], host_id=world.host['id'],
        repository_id=repository_id, path='/roadmap', branch_name='main', base_commit_oid='c' * 40, status='ready')
    premerge = register_check(world.conn, world.host_actor, CheckSubmission(
        workspace_id=workspace['id'], report=report()))
    reviewed_gate(world, review, 'a' * 40, 3,
                  ['statement-fidelity', 'definitions', 'decomposition', 'library-api'])

    view = objective_view(world.conn, world.actor, document['id'], world.service.config)
    readiness = view['initial_baseline_readiness']
    assert view['checks'] == [] and view['current_baseline_id'] is None
    assert readiness['required_source_commit_oid'] == 'c' * 40
    assert readiness['required_manifest_digest'] == premerge['report']['manifest_digest']
    assert readiness['verification_status'] == 'missing' and readiness['check_id'] is None
    assert readiness['blockers'][0]['code'] == 'indexed_source_check_missing'
    action = readiness['next_actions'][0]
    assert action['path'] == '/api/v3/milestones/verifications' and action['method'] == 'POST'
    assert action['request_template'] == {'source_commit_oid': 'c' * 40}
    assert action['required_inputs'] == ['workspace_id', 'base_commit_oid']
    assert action['schema_lookup'] == {'section': 'operations', 'name': 'milestone_verification'}

    current = register_check(world.conn, world.host_actor, CheckSubmission(workspace_id=workspace['id'],
        report=report(source_commit_oid='c' * 40, base_commit_oid='a' * 40, kind=kind,
                      base_manifest_digest=premerge['report']['manifest_digest'])))
    view = objective_view(world.conn, world.actor, document['id'], world.service.config)
    readiness = view['initial_baseline_readiness']
    assert readiness['verification_status'] == 'available' and readiness['check_id'] == str(current['id'])
    assert readiness['blockers'] == [] and readiness['next_actions'] == []
    assert readiness['approval_requires_human'] is True
    assert readiness['approval_readiness_evaluated'] is False
    assert readiness['initial_baseline_accepted'] is False and view['current_baseline_id'] is None


@pytest.mark.parametrize('receipt_changes', [{'kind': 'route'}, {'manifest_digest': 'f' * 64}])
def test_unfinished_contract_does_not_request_a_final_packet_build(world, receipt_changes):
    change(world.conn, 'project', world.project['id'], workflow='milestones')
    repository_id = world.document['source_repository_id']
    index_snapshot(world.conn, world.actor, repository_id, 'a' * 40, sources())
    document = world.conn.execute(select(tables['document']).where(
        tables['document'].c.source_path == 'objectives/example.md')).mappings().one()
    workspace = create(world.conn, 'workspace', project_id=world.project['id'], host_id=world.host['id'],
        repository_id=repository_id, path='/roadmap', branch_name='main', base_commit_oid='a' * 40, status='ready')
    register_check(world.conn, world.host_actor, CheckSubmission(workspace_id=workspace['id'],
        report=report(**receipt_changes)))
    readiness = objective_view(world.conn, world.actor, document['id'],
                               world.service.config)['initial_baseline_readiness']
    assert readiness['verification_status'] == 'missing'
    assert readiness['accepted_contract_observed'] is False
    assert [action['action'] for action in readiness['next_actions']] == ['complete_contract_review']
    assert all('request_template' not in action for action in readiness['next_actions'])


def test_reviewed_baseline_human_gate_launch_and_truthful_proof_status(world, review):
    change(world.conn, 'project', world.project['id'], workflow='milestones')
    world.policy = change(world.conn, 'review_policy', world.policy['id'], phases=['preprocessing', 'formalization'])
    repository_id = world.document['source_repository_id']
    data = sources()
    index_snapshot(world.conn, world.actor, repository_id, 'a' * 40, data)
    document = world.conn.execute(select(tables['document']).where(
        tables['document'].c.source_path == 'objectives/example.md')).mappings().one()
    change(world.conn, 'mission', world.mission['id'], roadmap_document_id=document['id'])
    workspace = create(world.conn, 'workspace', project_id=world.project['id'], host_id=world.host['id'],
        repository_id=repository_id, path='/roadmap', branch_name='main', base_commit_oid='a' * 40, status='ready')
    def receipt(**changes):
        return register_check(world.conn, world.host_actor, CheckSubmission(workspace_id=workspace['id'], report=report(**changes)))
    receipt(source_commit_oid='b' * 40, kind='route', manifest_digest=source_manifest(sources(False))['digest'])
    check = receipt(targets={'Example.result': ['sorryAx'], 'Example.endpoint': ['sorryAx']},
                    types={'Example.result': [], 'Example.endpoint': []})
    route_item, route_gate = reviewed_gate(world, review, 'b' * 40, 2, ['decomposition'])
    item, gate = reviewed_gate(world, review, 'a' * 40, 3, ['statement-fidelity', 'definitions', 'decomposition', 'library-api'])
    assert not review_panel(world.conn, item, world.policy)['missing']
    proposed = AcceptBaseline(document_id=document['id'], expected_revision=document['revision'], check_id=check['id'],
        route_gate_id=route_gate['id'], contract_gate_id=gate['id'], note='Approve the source-faithful architecture.')
    with pytest.raises(DomainError, match='human approval'):
        accept_baseline(world.conn, review['actor'], world.service, proposed)
    with pytest.raises(DomainError, match='bibliography'):
        accept_baseline(world.conn, world.actor, world.service, proposed)
    create(world.conn, 'reference', project_id=world.project['id'], cite_key='source', kind='book', title='Primary source')
    before = objective_view(world.conn, world.actor, document['id'], world.service.config)
    assert before['nodes'][0]['statement_status'] == 'accepted'
    assert before['nodes'][0]['proof_status'] == 'open' and before['current_baseline_id'] is None
    baseline = accept_baseline(world.conn, world.actor, world.service, proposed)
    require_baseline(world.conn, baseline)
    run = world.scheduler.run(world.conn, world.actor, models.RunCreate(orchestration="legacy", mission_id=world.mission['id'],
        host_ids=[world.host['id']], phase={'kind': 'formalization', 'roadmap_snapshot_id': baseline['id']}))
    assert run['adopted_roadmap_snapshot_id'] == baseline['id']
    # A self-reported complete claim cannot establish proof closure.
    data['nodes/example/m01.md'] = data['nodes/example/m01.md'].replace('"statement": "accepted"',
        '"statement": "accepted", "proof": "complete"')
    index_snapshot(world.conn, world.actor, repository_id, 'c' * 40, data)
    view = objective_view(world.conn, world.actor, document['id'], world.service.config)
    assert view['current_baseline_id'] == str(baseline['id']) and view['nodes'][0]['proof_status'] == 'open'
    assert view['initial_baseline_readiness']['verification_status'] == 'not_required'
    assert view['initial_baseline_readiness']['next_actions'] == []
    proof = receipt(kind='proof', comparator_passed=True, implementation_commit_oid='d' * 40,
        targets={'Example.result': ['sorryAx'], 'Example.endpoint': ['sorryAx']},
        types={'Example.result': [], 'Example.endpoint': []}, direct_admissions=['Example.result'])
    data['nodes/example/m01.md'] = data['nodes/example/m01.md'].replace('"proof": "complete"',
        f'"proof": "complete", "proof_check_id": "{proof["id"]}"')
    index_snapshot(world.conn, world.actor, repository_id, 'e' * 40, data)
    assert objective_view(world.conn, world.actor, document['id'], world.service.config)['nodes'][0]['proof_status'] == 'open'
    proof = receipt(kind='proof', comparator_passed=True, implementation_commit_oid='f' * 40,
        targets={'Example.result': ['sorryAx'], 'Example.endpoint': ['sorryAx']},
        types={'Example.result': [], 'Example.endpoint': []})
    data['nodes/example/m01.md'] = data['nodes/example/m01.md'].replace(
        data['nodes/example/m01.md'].split('"proof_check_id": "')[1].split('"')[0], str(proof['id']))
    index_snapshot(world.conn, world.actor, repository_id, 'f' * 40, data)
    assert objective_view(world.conn, world.actor, document['id'], world.service.config)['nodes'][0]['proof_status'] == 'conditional'
    data['milestones/Example/Definitions/Basic.lean'] += 'def Example.extra : Nat := 0\n'
    index_snapshot(world.conn, world.actor, repository_id, '1' * 40, data)
    assert objective_view(world.conn, world.actor, document['id'], world.service.config)['nodes'][0]['proof_status'] == 'needs_recheck'
    revised = receipt(source_commit_oid='1' * 40, manifest_digest=source_manifest(data)['digest'],
        ancestor_commits=['1' * 40, 'a' * 40, 'b' * 40],
        targets={'Example.result': ['sorryAx'], 'Example.endpoint': ['sorryAx']}, types={'Example.result': [], 'Example.endpoint': []})
    _, correction_gate = reviewed_gate(world, review, '1' * 40, 4, ['statement-fidelity', 'definitions', 'decomposition', 'library-api'])
    document = get(world.conn, 'document', document['id'])
    correction = AcceptBaseline(document_id=document['id'], expected_revision=document['revision'],
        check_id=revised['id'], route_gate_id=route_gate['id'], contract_gate_id=correction_gate['id'],
        previous_snapshot_id=baseline['id'], note='Add the reviewed useful shared definition.')
    replacement = accept_baseline(world.conn, review['actor'], world.service, correction)
    assert get(world.conn, 'run', run['id'])['adopted_roadmap_snapshot_id'] == baseline['id']
    adopted = world.command('adopt_roadmap_snapshot', run, snapshot_id=str(replacement['id']))
    assert adopted['adopted_roadmap_snapshot_id'] == replacement['id']


def test_only_owning_host_can_attest_and_definitions_cannot_be_admitted(world):
    workspace = create(world.conn, 'workspace', project_id=world.project['id'], host_id=world.host['id'],
        repository_id=world.document['source_repository_id'], path='/tmp/roadmap', branch_name='main',
        base_commit_oid='a' * 40, status='ready')
    data = CheckSubmission(workspace_id=workspace['id'], report=report())
    for actor in [world.actor, Actor(uuid4(), 'agent', {}, 'execution_token')]:
        with pytest.raises(DomainError, match='host credential'):
            register_check(world.conn, actor, data)
    first = register_check(world.conn, world.host_actor, data)
    assert register_check(world.conn, world.host_actor, data)['id'] == first['id']
    data.report = report(definitions={'Example.object': ['sorryAx']})
    with pytest.raises(DomainError, match='foundation-only'):
        register_check(world.conn, world.host_actor, data)
    data.report = report(proof_claims={'example-m01': {'status': 'complete', 'declarations': ['Example.result']}})
    with pytest.raises(DomainError, match='Comparator and axiom evidence'):
        register_check(world.conn, world.host_actor, data)


def test_verification_queue_lease_fencing_and_result_binding(world):
    from datetime import timedelta
    workspace = create(world.conn, 'workspace', project_id=world.project['id'], host_id=world.host['id'],
        repository_id=world.document['source_repository_id'], path='/roadmap', branch_name='main',
        base_commit_oid='a' * 40, status='ready')
    change(world.conn, 'project', world.project['id'], workflow='milestones')
    request = milestone_jobs.VerificationRequest(workspace_id=workspace['id'], source_commit_oid='a' * 40,
        base_commit_oid='b' * 40)
    with pytest.raises(DomainError, match='Enable milestone_checks'):
        milestone_jobs.queue(world.conn, world.actor, request)
    change(world.conn, 'host', world.host['id'], health={'capabilities': {'milestone_verification': 1}})
    job = milestone_jobs.queue(world.conn, world.actor, request)
    assert milestone_jobs.queue(world.conn, world.actor, request)['id'] == job['id']
    claiming = milestone_jobs.Claim(host_id=world.host['id'])
    with pytest.raises(DomainError, match='host credential'):
        milestone_jobs.claim(world.conn, world.actor, claiming)
    first = milestone_jobs.claim(world.conn, world.host_actor, claiming)
    assert milestone_jobs.claim(world.conn, world.host_actor, claiming) is None
    change(world.conn, 'milestone_job', job['id'], lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    second = milestone_jobs.claim(world.conn, world.host_actor, claiming)
    assert second['claim_token'] != first['claim_token'] and second['attempts'] == 2
    with pytest.raises(DomainError, match='lease is no longer'):
        milestone_jobs.finish(world.conn, world.host_actor, job['id'],
            milestone_jobs.Finish(claim_token=first['claim_token'], report=report()))
    with pytest.raises(DomainError, match='immutable inputs'):
        milestone_jobs.finish(world.conn, world.host_actor, job['id'],
            milestone_jobs.Finish(claim_token=second['claim_token'], report=report(source_commit_oid='c' * 40)))
    result = milestone_jobs.finish(world.conn, world.host_actor, job['id'],
        milestone_jobs.Finish(claim_token=second['claim_token'], report=report()))
    assert result['status'] == 'completed' and result['check_id']
    assert milestone_jobs.queue(world.conn, world.actor, request)['id'] == job['id']


def test_milestone_projects_cannot_forge_generic_snapshots(world):
    change(world.conn, 'project', world.project['id'], workflow='milestones')
    artifact = create(world.conn, 'artifact', project_id=world.project['id'], kind='blob', content={})
    with pytest.raises(DomainError, match='verified milestone'):
        create_catalog(world.conn, world.actor, 'roadmap_snapshot', {'project_id': world.project['id'],
            'roadmap_document_id': world.document['id'], 'source_commit_oid': 'a' * 40,
            'graph_manifest_artifact_id': artifact['id']})
    fake = {'project_id': world.project['id'], 'status': 'frozen', 'id': uuid4()}
    with pytest.raises(DomainError, match='approved, verified'):
        require_baseline(world.conn, fake)
    change(world.conn, 'project', world.project['id'], workflow='legacy')
    require_baseline(world.conn, fake)


@pytest.mark.parametrize('table_content', [None, 'Outdated milestone table'])
def test_route_check_names_generated_table_path_and_renderer(tmp_path, table_content):
    root = tmp_path / 'roadmap'
    root.mkdir()
    def command(*args):
        return subprocess.run(args, cwd=root, check=True, capture_output=True, text=True).stdout
    command('git', 'init', '-q')
    command('git', 'config', 'user.name', 'Test')
    command('git', 'config', 'user.email', 'test@example.invalid')
    command('git', 'commit', '--allow-empty', '-qm', 'Initial')
    base = command('git', 'rev-parse', 'HEAD').strip()
    data = sources(False)
    table_path = 'milestones/Example/milestones.md'
    if table_content is None:
        del data[table_path]
    else:
        data[table_path] = table_content
    for path, content in data.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    command('git', 'add', '.')
    command('git', 'commit', '-qm', 'Route with missing or stale generated table')
    with pytest.raises(ValueError) as error:
        verify(root, base, LeanBuildPolicy(root=tmp_path / 'build', minimum_free_bytes=0))
    message = str(error.value)
    assert f'table in {table_path}' in message
    assert '(objective: objectives/example.md)' in message
    assert 'python -m archon_horizon.pipeline.worker.milestone_verify --root <checkout> --tables' in message
    assert 'verify its new head' in message


@pytest.mark.skipif(not shutil.which('lake'), reason='Lean toolchain unavailable')
def test_real_lean_skeleton_and_hidden_definition_admission(tmp_path):
    root = tmp_path / 'roadmap'
    root.mkdir()
    def command(*args):
        return subprocess.run(args, cwd=root, check=True, capture_output=True, text=True).stdout
    command('git', 'init', '-q')
    command('git', 'config', 'user.name', 'Test')
    command('git', 'config', 'user.email', 'test@example.invalid')
    (root / '.gitignore').write_text('.lake/\n')
    command('git', 'add', '.')
    command('git', 'commit', '-qm', 'Initial')
    base = command('git', 'rev-parse', 'HEAD').strip()
    for path, text in sources().items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    command('git', 'add', '.')
    command('git', 'commit', '-qm', 'Skeleton')
    # The full Lean environment audit is sensitive to shared-filesystem latency.
    policy = LeanBuildPolicy(root=tmp_path / 'build', minimum_free_bytes=0, timeout_seconds=240)
    checked = verify(root, base, policy)
    assert checked['kind'] == 'contract'
    assert checked['targets']['Example.result'] == ['sorryAx']
    assert set(checked['direct_admissions']) == {'Example.result', 'Example.endpoint'}
    assert checked['definitions']['Example.object'] == []
    renewal = []
    def post(path, payload):
        renewal.append(path)
        return SimpleNamespace(raise_for_status=lambda: None)
    daemon = SimpleNamespace(harnesses={'build': SimpleNamespace(lean_build=policy)},
        workspace_roots=(tmp_path,), publication_remotes={}, _publication_headers={},
        transport=SimpleNamespace(_post=post), _progress=lambda *args, **kwargs: None)
    job = {'id': str(uuid4()), 'claim_token': str(uuid4()), 'workspace': {'path': str(root), 'repository_id': str(uuid4())},
        'solution_workspace': None, 'request': {'source_commit_oid': checked['source_commit_oid'], 'base_commit_oid': base}}
    isolated = execute_verification(daemon, job, threading.Event())
    assert isolated['manifest_digest'] == checked['manifest_digest'] and renewal
    assert command('git', 'status', '--porcelain') == ''
    assert not list(policy.root.glob('milestone-job-*'))
    (root / 'milestones/Example/Definitions/Basic.lean').write_text('def Example.object : Nat := by sorry\n')
    command('git', 'add', '.')
    command('git', 'commit', '-qm', 'Bad definition')
    with pytest.raises(ValueError, match='Admitted or nonstandard'):
        verify(root, base, policy)
