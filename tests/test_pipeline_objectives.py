"""Objective ownership regressions: duplicate observations must not create owners."""
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from test_pipeline_service import world, service_database
from archon_horizon.pipeline import models
from archon_horizon.pipeline.auth import Actor
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.execution.scheduler import Scheduler
from archon_horizon.pipeline.execution import objectives
from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.review import demand


def launch(world):
    world.scheduler = Scheduler(world.service)
    return world.scheduler.run(world.conn, world.actor, models.RunCreate(
        objective_id=world.document['id'], host_ids=[world.host['id']], requested_phases=['preprocessing']))


def assignments(world, run):
    return list(world.conn.execute(select(tables['assignment']).where(
        tables['assignment'].c.run_id == run['id']).order_by(tables['assignment'].c.number)).mappings())


def agent(world, grant):
    principal = world.conn.execute(select(tables['principal']).where(
        tables['principal'].c.execution_id == UUID(grant['execution_id']))).mappings().one()
    return Actor(principal['id'], 'agent', {'execution_id': grant['execution_id']}, 'execution_token')


def test_objective_only_launch_has_one_bounded_planner_and_one_successor(world):
    run = launch(world)
    first = assignments(world, run)
    assert len(first) == 1 and first[0]['category'] == 'work'
    assert first[0]['mission_id'] != run['mission_id']
    grant = world.claim()
    assert grant and grant['max_parallel_subagents'] == 0
    after = assignments(world, run)
    assert len(after) == 2
    assert {a['recurrence_key'].rsplit(':', 1)[1] for a in after} == {'active', 'next'}
    for _ in range(5):
        objectives.reconcile(world.scheduler, world.conn, world.actor, datetime.now(timezone.utc))
    assert len(assignments(world, run)) == 2
    with pytest.raises(DomainError, match='existing objective'):
        launch(world)


def test_same_mission_and_renamed_unchanged_delegation_are_rejected(world):
    run = launch(world)
    grant = world.claim()
    actor = agent(world, grant)
    current = get(world.conn, 'assignment', UUID(grant['assignment_id']))
    with pytest.raises(DomainError) as error:
        world.service.assignment(world.conn, actor, models.AssignmentCreate(
            run_id=run['id'], mission_id=current['mission_id'], parent_id=current['id']))
    assert error.value.code == 'child_mission_required'
    mission = get(world.conn, 'mission', current['mission_id'])
    child = objectives.child_mission(world.scheduler, world.conn, run, 'Renamed', mission['objective'], ['Account for outcome'])
    with pytest.raises(DomainError) as error:
        world.service.assignment(world.conn, actor, models.AssignmentCreate(
            run_id=run['id'], mission_id=child['id'], parent_id=current['id']))
    assert error.value.code == 'unchanged_mission'


def test_recovery_preserves_owner_attempts_and_unconfirmed_capacity(world):
    run = launch(world)
    grant = world.claim()
    owner = get(world.conn, 'assignment', UUID(grant['assignment_id']))
    now = datetime.now(timezone.utc)
    values = objectives.finish_values(world.scheduler, world.conn, owner, run, 'failed',
        {'code': 'connection_error'}, now, stopping=False, reason=None, journal_pending=False)
    assert values['status'] == 'pending' and values['recovery_attempts'] == 1 and values['retry_at'] > now
    owner = change(world.conn, 'assignment', owner['id'], **values)
    values = objectives.finish_values(world.scheduler, world.conn, owner, run, 'failed',
        {'code': 'authentication_failed', 'message': 'Repair credential'}, now,
        stopping=False, reason=None, journal_pending=False)
    assert values['pause_reason'] and values['recovery_attempts'] == 2
    execution = change(world.conn, 'execution', UUID(grant['execution_id']), status='lost',
        finished_at=now-timedelta(hours=1), stop_confirmed_at=None)
    assert world.conn.execute(select(tables['execution'].c.id).where(
        tables['execution'].c.id == execution['id'], world.scheduler.execution_busy_condition())).first()
    change(world.conn, 'execution', execution['id'], stop_confirmed_at=now)
    assert not world.conn.execute(select(tables['execution'].c.id).where(
        tables['execution'].c.id == execution['id'], world.scheduler.execution_busy_condition())).first()


def test_review_demand_coalesces_and_old_settlement_cannot_erase_new_request(world):
    run = launch(world)
    item = create(world.conn, 'forge_item', repository_id=world.document['source_repository_id'],
        origin_run_id=run['id'], review_phase='preprocessing', remote_number=1,
        kind='pull_request', title='Milestones', status='open', labels=['awaiting-review'],
        head_commit_oid='b'*40, target_branch='main', observed_at=func.now())
    now = datetime.now(timezone.utc)
    first = demand.observe(world.conn, run, item, now)
    for _ in range(5):
        assert demand.observe(world.conn, run, item, now)['generation'] == first['generation']
        demand.reconcile(world.scheduler, world.conn, world.actor, now)
    owners = [a for a in assignments(world, run) if a['category'] == 'maintenance']
    assert len(owners) == 1
    new = demand.request(world.conn, world.actor, world.service, item['id'], 'New evidence available')
    demand.reconcile(world.scheduler, world.conn, world.actor, now)
    assert len([a for a in assignments(world, run) if a['category'] == 'maintenance']) == 1
    projection = demand.label_projection(world.conn, item['id'], {
        'add': [], 'remove': ['awaiting-review'], 'review_generation': first['generation']})
    assert projection['add'] == ['awaiting-review'] and projection['remove'] == []
    assert new['generation'] == first['generation'] + 1


def test_settled_label_delivery_lag_is_not_a_new_review_round(world):
    run = launch(world)
    item = create(world.conn, 'forge_item', repository_id=world.document['source_repository_id'],
        origin_run_id=run['id'], review_phase='preprocessing', remote_number=2,
        kind='issue', title='Decision', status='open', labels=['awaiting-review'], observed_at=func.now())
    now = datetime.now(timezone.utc)
    old = demand.observe(world.conn, run, item, now)
    world.conn.execute(update(tables['review_demand']).where(tables['review_demand'].c.forge_item_id == item['id'])
        .values(handled_generation=old['generation'], attention=False))
    assert demand.observe(world.conn, run, item, now)['generation'] == old['generation']
    item = change(world.conn, 'forge_item', item['id'], labels=[])
    demand.observe(world.conn, run, item, now)
    item = change(world.conn, 'forge_item', item['id'], labels=['awaiting-review'])
    assert demand.observe(world.conn, run, item, now)['generation'] == old['generation']+1


def test_resource_probe_honors_nested_cgroup_memory_and_unknown_metrics(tmp_path):
    from archon_horizon.pipeline.worker.resource_health import ResourcePolicy, observe
    proc, cg = tmp_path/'proc', tmp_path/'cgroup'
    (proc/'self').mkdir(parents=True)
    (cg/'job').mkdir(parents=True)
    (proc/'meminfo').write_text('MemAvailable: 4000000 kB\n')
    (proc/'self/cgroup').write_text('0::/job\n')
    (cg/'memory.max').write_text('2000')
    (cg/'memory.current').write_text('1500')
    (cg/'job/memory.max').write_text('max')
    result = observe(ResourcePolicy(minimum_available_memory_bytes=1000), proc=proc, cgroup=cg)
    assert result['status'] == 'resource_pressure' and result['available_memory_bytes'] == 500
    assert result['psi_avg10']['io'] is None


def test_category_pending_limit_and_defaults_are_independent(world):
    run = launch(world)
    policies = dict(run['queue_policies'])
    policies['work'] = {**policies['work'], 'max_pending': 1, 'model_options': {'model': 'work-model'}}
    policies['maintenance'] = {**policies['maintenance'], 'model_options': {'model': 'review-model'}}
    run = change(world.conn, 'run', run['id'], queue_policies=policies)
    mission = objectives.child_mission(world.scheduler, world.conn, run, 'Bounded work', 'Prove one helper', ['Verified proof'])
    with pytest.raises(DomainError) as error:
        world.service.assignment(world.conn, world.actor, models.AssignmentCreate(run_id=run['id'], mission_id=mission['id']))
    assert error.value.code == 'queue_full'
    review = world.service.assignment(world.conn, world.actor, models.AssignmentCreate(
        run_id=run['id'], mission_id=mission['id'], role='maintainer', category='maintenance'))
    assert review['model_options']['model'] == 'review-model'
    with pytest.raises(DomainError) as error:
        world.service.assignment(world.conn, world.actor, models.AssignmentCreate(
            run_id=run['id'], mission_id=mission['id'], role='worker', category='maintenance'))
    assert error.value.code == 'category_role_mismatch'


def test_worker_can_request_one_bounded_maintenance_decision(world):
    run = launch(world)
    grant = world.claim()
    actor = agent(world, grant)
    args = {'note': 'Assess whether preprocessing is ready to accept',
            'evidence': [{'kind': 'document', 'id': str(world.document['id'])}]}
    first = objectives.request_maintenance(world.scheduler, world.conn, actor, run, args)
    assert first['role'] == 'maintainer' and first['mission_id'] != UUID(grant['assignment_id'])
    assert objectives.request_maintenance(world.scheduler, world.conn, actor, run, args)['id'] == first['id']
    assert len(assignments(world, run)) == 3  # Running planner, successor, decision.


def test_acceptance_waits_for_physical_stop_and_completes_without_another_planner(world):
    run = launch(world)
    planner = assignments(world, run)[0]
    change(world.conn, 'assignment', planner['id'], status='cancelled', finished_at=func.now())
    change(world.conn, 'mission', planner['mission_id'], status='cancelled', closed_at=func.now(), closure_note='Superseded by acceptance')
    for row in world.ledger(planner['id']):
        change(world.conn, 'obligation', row['id'], status='done', resolution={'kind': 'completed', 'note': 'Planning decision accounted for', 'evidence': []})
    accepted = objectives.accept_phase(world.scheduler, world.conn, world.actor, run,
        {'note': 'Requested roadmap outcome accepted', 'evidence': [{'kind': 'document', 'id': str(world.document['id'])}]})
    assert accepted['pending_phase'] == {'kind': 'complete'}
    objectives.advance(world.scheduler, world.conn, accepted, datetime.now(timezone.utc))
    assert get(world.conn, 'run', run['id'])['status'] == 'draining'
    assert len(assignments(world, run)) == 1


def test_native_reservations_consume_category_and_physical_capacity(world):
    run = launch(world)
    planner = assignments(world, run)[0]
    change(world.conn, 'assignment', planner['id'], status='cancelled')
    world.conn.execute(update(tables['host_harness']).where(tables['host_harness'].c.host_id == world.host['id']).values(execution_slots=4))
    # The launch's provider limit is independently configured; permit 4 slots here.
    world.conn.execute(update(tables['resource_limit']).values(max_concurrent=4))
    mission = objectives.child_mission(world.scheduler, world.conn, run, 'Proof', 'Prove a bounded theorem', ['Proof checked'])
    item = world.service.assignment(world.conn, world.actor, models.AssignmentCreate(run_id=run['id'], mission_id=mission['id']))
    grant = world.claim()
    assert grant['assignment_id'] == str(item['id']) and grant['max_parallel_subagents'] == 2
    execution = get(world.conn, 'execution', UUID(grant['execution_id']))
    assert execution['native_capacity'] == 2
    assert world.conn.execute(select(func.sum(tables['resource_claim'].c.units)).where(
        tables['resource_claim'].c.execution_id == execution['id'])).scalar_one() == 3


def test_retirement_refuses_a_suspended_owner(world):
    from archon_horizon.pipeline.operations.workspace_retention import retire
    run = launch(world)
    grant = world.claim()
    workspace = get(world.conn, 'workspace', UUID(grant['workspace_id']))
    workspace = change(world.conn, 'workspace', workspace['id'], path=world.host['workspace_root']+'/assignments/'+grant['assignment_id'])
    change(world.conn, 'assignment', UUID(grant['assignment_id']), status='pending', pause_reason='Diagnose')
    with pytest.raises(DomainError) as error:
        retire(world.conn, world.actor, workspace, world.service, 'Reclaim space')
    assert error.value.code == 'workspace_retained'


def test_library_receipt_rejects_hidden_admissions(world):
    from archon_horizon.pipeline.projects.milestones import CheckReport, CheckSubmission, register_check
    library = create(world.conn, 'repository', project_id=world.project['id'], slug='library',
        integration_id=world.workspace_repo['integration_id'], remote_id='library', default_branch='main', purpose='library')
    workspace = create(world.conn, 'workspace', project_id=world.project['id'], repository_id=library['id'], host_id=world.host['id'],
        path=world.host['workspace_root']+'/library', branch_name='main', base_commit_oid='a'*40, status='ready')
    fields = dict(source_commit_oid='b'*40, base_commit_oid='a'*40, kind='library',
        manifest_digest='a'*64, base_manifest_digest='b'*64, ancestor_commits=['a'*40], milestone_keys=[], objective_paths=[],
        compiled=True, toolchain='lean4', definitions={'Hidden.lemma': ['sorryAx']})
    with pytest.raises(DomainError, match='foundation-only'):
        register_check(world.conn, world.host_actor, CheckSubmission(workspace_id=workspace['id'], report=CheckReport(**fields)))
    fields['definitions'] = {'Library.theorem': ['propext']}
    receipt = register_check(world.conn, world.host_actor, CheckSubmission(workspace_id=workspace['id'], report=CheckReport(**fields)))
    assert receipt['kind'] == 'library'


def test_postprocessing_namespace_preserves_original_milestone_identity():
    import json
    from test_pipeline_milestones import sources, markdown
    from archon_horizon.pipeline.projects.milestone_sources import source_manifest
    original = sources(contract=False)
    data = {'label': 'postprocessing-m01', 'milestone': {'id': 'M01', 'objective': 'objectives/example.md',
        'namespace': 'postprocessing', 'provenance': ['example-m01']}}
    result = source_manifest({**original, 'nodes/postprocessing/m01.md': markdown(data)})
    assert len(result['nodes']) == 2
    assert 'namespace' not in result['nodes']['example-m01']['milestone']
    assert result['nodes']['postprocessing-m01']['milestone']['provenance'] == ['example-m01']


def test_worker_cleanup_preserves_unpublished_local_commits(tmp_path):
    import subprocess
    from archon_horizon.pipeline.worker.workspace_retention import cleanup
    def git(root, *args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()
    source = tmp_path/'remote'
    source.mkdir()
    git(source, 'init', '-q', '-b', 'main')
    git(source, 'config', 'receive.denyCurrentBranch', 'updateInstead')
    git(source, 'config', 'user.name', 'Test')
    git(source, 'config', 'user.email', 'test@example.invalid')
    (source/'proof.lean').write_text('theorem ready : True := True.intro\n')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'Published')
    from uuid import uuid4
    root = tmp_path/'workspaces'
    path = root/'assignments'/str(uuid4())
    path.parent.mkdir(parents=True)
    subprocess.run(['git', 'clone', '-q', str(source), str(path)], check=True)
    git(path, 'config', 'user.name', 'Test')
    git(path, 'config', 'user.email', 'test@example.invalid')
    (path/'local.md').write_text('Useful unpublished route')
    git(path, 'add', '.')
    git(path, 'commit', '-qm', 'Unpublished')
    with pytest.raises(ValueError, match='commits absent'):
        cleanup({'path': str(path), 'default_branch': 'main'}, [root], str(source))
    assert (path/'local.md').exists()
    git(path, 'push', '-q', 'origin', 'main')
    # Ignored proof notes are unique work too; a clean Git status is insufficient.
    (path/'.git/info/exclude').write_text('.notes/\n')
    (path/'.notes').mkdir()
    (path/'.notes/route.md').write_text('A useful unpublished idea')
    with pytest.raises(ValueError, match='ignored data'):
        cleanup({'path': str(path), 'default_branch': 'main'}, [root], str(source))
    assert (path/'.notes/route.md').exists()
    (path/'.notes/route.md').unlink()
    (path/'.notes').rmdir()
    cleanup({'path': str(path), 'default_branch': 'main'}, [root], str(source))
    assert not path.exists()


def test_legacy_launch_cannot_compete_with_an_active_objective(world):
    launch(world)
    with pytest.raises(DomainError) as error:
        world.run()
    assert error.value.code == 'objective_already_active'


def test_planner_idle_pause_wakes_only_on_substantive_evidence(world):
    run = launch(world)
    grant = world.claim()
    item = get(world.conn, 'assignment', grant['assignment_id'])
    now = datetime.now(timezone.utc)
    for _ in range(world.scheduler.config.planner_max_unchanged_passes):
        objectives.completed(world.scheduler, world.conn, item, run, now)
    rule = get(world.conn, 'automation', item['automation_id'])
    assert not rule['enabled'] and rule['pause_reason'] == 'idle'
    for _ in range(5):
        objectives.reconcile(world.scheduler, world.conn, world.actor, now)
    assert not get(world.conn, 'automation', rule['id'])['enabled']
    assert len(assignments(world, run)) == 2
    change(world.conn, 'document', world.document['id'], source_commit_oid='b'*40)
    objectives.reconcile(world.scheduler, world.conn, world.actor, now)
    rule = get(world.conn, 'automation', rule['id'])
    assert rule['enabled'] and rule['no_progress_count'] == 0


def test_explicit_automation_pause_survives_completion_and_new_evidence(world):
    run = launch(world)
    grant = world.claim()
    item = get(world.conn, 'assignment', grant['assignment_id'])
    rule = change(world.conn, 'automation', item['automation_id'], enabled=False, pause_reason='operator')
    now = datetime.now(timezone.utc)
    objectives.completed(world.scheduler, world.conn, item, run, now)
    change(world.conn, 'document', world.document['id'], source_commit_oid='b'*40)
    objectives.reconcile(world.scheduler, world.conn, world.actor, now)
    assert not get(world.conn, 'automation', rule['id'])['enabled']
    assert get(world.conn, 'automation', rule['id'])['pause_reason'] == 'operator'
    successor = next(a for a in assignments(world, run) if a['id'] != item['id'])
    assert successor['pause_reason']


def test_external_review_can_select_objective_and_closed_item_stops_owner(world):
    run = launch(world)
    item = create(world.conn, 'forge_item', repository_id=world.document['source_repository_id'],
        remote_number=20, kind='issue', title='Human question', status='open', labels=[], observed_at=func.now())
    request = world.command('request_review', item, note='Assess the proposed strategy', run_id=str(run['id']))
    assert request['run_id'] == run['id']
    now = datetime.now(timezone.utc)
    demand.reconcile(world.scheduler, world.conn, world.actor, now)
    request = world.conn.execute(select(tables['review_demand']).where(
        tables['review_demand'].c.forge_item_id == item['id'])).mappings().one()
    owner = request['assignment_id']
    assert owner and get(world.conn, 'forge_item', item['id'])['origin_run_id'] is None
    change(world.conn, 'forge_item', item['id'], status='closed')
    demand.reconcile(world.scheduler, world.conn, world.actor, now)
    assert get(world.conn, 'assignment', owner)['status'] == 'cancelled'
    request = world.conn.execute(select(tables['review_demand']).where(
        tables['review_demand'].c.forge_item_id == item['id'])).mappings().one()
    assert request['handled_generation'] == request['generation'] and not request['attention']
    change(world.conn, 'forge_item', item['id'], status='open', labels=['awaiting-review'])
    demand.reconcile(world.scheduler, world.conn, world.actor, now)
    renewed = world.conn.execute(select(tables['review_demand']).where(
        tables['review_demand'].c.forge_item_id == item['id'])).mappings().one()
    assert renewed['assignment_id'] != owner and renewed['generation'] > request['generation']


def test_external_label_observation_has_one_review_owner(world):
    run = launch(world)
    item = create(world.conn, 'forge_item', repository_id=world.document['source_repository_id'],
        remote_number=21, kind='pull_request', title='Human roadmap', status='open', labels=['awaiting-review'],
        head_commit_oid='b'*40, target_branch='main', observed_at=func.now())
    for _ in range(5):
        demand.reconcile(world.scheduler, world.conn, world.actor, datetime.now(timezone.utc))
    request = world.conn.execute(select(tables['review_demand']).where(
        tables['review_demand'].c.forge_item_id == item['id'])).mappings().one()
    assert request['run_id'] == run['id'] and request['assignment_id']
    assert len([a for a in assignments(world, run) if a['category'] == 'maintenance']) == 1


def test_shared_provider_circuit_and_diagnosed_resume_preserve_retry_history(world):
    run = launch(world)
    policies = dict(run['queue_policies'])
    policies['work'] = {**policies['work'], 'retry_policy': {
        **policies['work']['retry_policy'], 'max_recovery_attempts': 1}}
    change(world.conn, 'run', run['id'], queue_policies=policies)
    first = world.claim()
    retained = first['provider_thread_record_id']
    for attempt in range(2):
        claim = first if attempt == 0 else world.claim()
        assert claim['assignment_id'] == first['assignment_id']
        assert claim['provider_thread_record_id'] == retained
        item = world.scheduler.finish(world.conn, world.host_actor,
            get(world.conn, 'execution', claim['execution_id']), 'failed', {'code': 'connection_error'})
        assert item['recovery_attempts'] == attempt + 1
        if attempt == 0:
            change(world.conn, 'assignment', item['id'], retry_at=None)
            world.conn.execute(update(tables['resource_limit']).values(cooldown_until=None))
    account = world.conn.execute(select(tables['resource_limit']).where(
        tables['resource_limit'].c.kind == 'provider_account')).mappings().one()
    assert account['circuit_open'] and item['pause_reason']
    resumed = world.command('resume_session', item, note='Provider connection repaired')
    assert resumed['recovery_attempts'] == 2
    assert 'circuit' in world.scheduler.admission_blocker(world.conn, resumed)
    assert world.claim() is None  # A session repair does not reset the account circuit.
    world.command('reset_circuit', account, note='Account endpoint checked and repaired')
    claim = world.claim()
    assert claim['assignment_id'] == first['assignment_id']
    assert claim['provider_thread_record_id'] == retained


def test_human_phase_boundary_does_not_advance_on_reconciliation(world):
    run = launch(world)
    run = change(world.conn, 'run', run['id'], auto_advance=False)
    planner = assignments(world, run)[0]
    world.scheduler.cancel(world.conn, world.actor, planner, 'Planning complete')
    change(world.conn, 'mission', planner['mission_id'], status='cancelled', closed_at=func.now(), closure_note='Planning complete')
    for row in world.ledger(planner['id']):
        change(world.conn, 'obligation', row['id'], status='done', resolution={
            'kind': 'completed', 'note': 'Planning accounted for', 'evidence': []})
    accepted = world.command('accept_phase', get(world.conn, 'run', run['id']),
        note='Preprocessing accepted', evidence=[{'kind': 'document', 'id': str(world.document['id'])}])
    assert accepted['status'] == 'paused' and accepted['pending_phase']
    objectives.reconcile(world.scheduler, world.conn, world.actor, datetime.now(timezone.utc))
    assert get(world.conn, 'run', run['id'])['status'] == 'paused'
    world.command('resume_run', get(world.conn, 'run', run['id']), note='Human accepts phase boundary')
    objectives.reconcile(world.scheduler, world.conn, world.actor, datetime.now(timezone.utc))
    assert get(world.conn, 'run', run['id'])['status'] == 'draining'


def test_real_library_check_rejects_hidden_sorry_in_changed_module(tmp_path):
    import shutil
    import subprocess
    from archon_horizon.pipeline.worker.lean_build import LeanBuildPolicy
    from archon_horizon.pipeline.worker.milestone_verify import verify_library
    if not shutil.which('lake'):
        pytest.skip('Lean toolchain unavailable')
    root = tmp_path/'library'
    root.mkdir()
    def command(*args):
        return subprocess.run(args, cwd=root, check=True, capture_output=True, text=True, timeout=240).stdout.strip()
    command('git', 'init', '-q')
    command('git', 'config', 'user.name', 'Test')
    command('git', 'config', 'user.email', 'test@example.invalid')
    (root/'.gitignore').write_text('.lake/\n')
    (root/'lean-toolchain').write_text('leanprover/lean4:v4.33.1\n')
    (root/'lakefile.toml').write_text('name = "library_test"\n[[lean_lib]]\nname = "Library"\n')
    (root/'lake-manifest.json').write_text('{"version":"1.2.0","packagesDir":".lake/packages","packages":[],"name":"library_test","lakeDir":".lake"}\n')
    (root/'Library.lean').write_text('theorem Library.ready : True := True.intro\n')
    command('git', 'add', '.')
    command('git', 'commit', '-qm', 'Accepted library')
    base = command('git', 'rev-parse', 'HEAD')
    (root/'Library.lean').write_text('theorem Library.ready : True := True.intro\ntheorem Library.more : 1 = 1 := rfl\n')
    command('git', 'add', '.')
    command('git', 'commit', '-qm', 'Complete contribution')
    # GPFS metadata and the full Lean environment audit can be slow. This is
    # an integration-test allowance, independent of production build settings.
    policy = LeanBuildPolicy(root=tmp_path/'build', minimum_free_bytes=0, timeout_seconds=240)
    receipt = verify_library(root, base, policy)
    assert receipt['kind'] == 'library' and receipt['base_commit_oid'] == base
    assert receipt['definitions']['Library.more'] == []
    (root/'Library.lean').write_text('theorem Library.ready : True := True.intro\ntheorem Library.hidden : False := by sorry\n')
    command('git', 'add', '.')
    command('git', 'commit', '-qm', 'Hidden admission')
    with pytest.raises(ValueError, match='Admitted or nonstandard'):
        verify_library(root, base, policy)


def test_accepting_maintainer_resumes_before_phase_transition(world):
    from archon_horizon.pipeline.commands import Command, execute
    run = launch(world)
    maintenance = objectives.request_maintenance(world.scheduler, world.conn, world.actor, run,
        {'note': 'Accept the prepared roadmap', 'evidence': [{'kind': 'document', 'id': str(world.document['id'])}]})
    planner = assignments(world, run)[0]
    world.scheduler.cancel(world.conn, world.actor, planner, 'Planning accounted for')
    change(world.conn, 'mission', planner['mission_id'], status='cancelled', closed_at=func.now(), closure_note='Planning accounted for')
    for row in world.ledger(planner['id']):
        change(world.conn, 'obligation', row['id'], status='done', resolution={
            'kind': 'completed', 'note': 'Planning accounted for', 'evidence': []})
    grant = world.claim()
    assert grant['assignment_id'] == str(maintenance['id'])
    actor = agent(world, grant)
    change(world.conn, 'provider_thread', UUID(grant['provider_thread_record_id']),
        status='available', provider_thread_id='retained-maintainer')
    current = get(world.conn, 'run', run['id'])
    accepted = execute(world.conn, actor, Command(operation='accept_phase', target_id=run['id'],
        expected_revision=current['revision'], args={'note': 'Outcome accepted',
        'evidence': [{'kind': 'document', 'id': str(world.document['id'])}]}), world.service, world.scheduler)
    assert accepted['pending_phase']['_owner_session_id'] == str(maintenance['id'])
    suspended = world.scheduler.finish(world.conn, world.host_actor, get(world.conn, 'execution', grant['execution_id']),
        'failed', {'code': 'connection_error'})
    objectives.reconcile(world.scheduler, world.conn, world.actor, datetime.now(timezone.utc))
    assert get(world.conn, 'run', run['id'])['status'] == 'active'
    change(world.conn, 'assignment', suspended['id'], retry_at=None)
    world.conn.execute(update(tables['resource_limit']).values(cooldown_until=None))
    resumed = world.claim()
    assert resumed['assignment_id'] == grant['assignment_id']
    assert resumed['provider_thread_record_id'] == grant['provider_thread_record_id']
    for row in world.ledger(maintenance['id']):
        change(world.conn, 'obligation', row['id'], status='done', resolution={
            'kind': 'completed', 'note': 'Decision delivered', 'evidence': []})
    completed = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, 'execution', resumed['execution_id']), 'succeeded')
    assert completed['status'] == 'completed'
    objectives.reconcile(world.scheduler, world.conn, world.actor, datetime.now(timezone.utc))
    assert get(world.conn, 'run', run['id'])['status'] == 'draining'


def test_dashboard_ledger_keeps_markdown_and_bounds_comments(world):
    from archon_horizon.pipeline.dashboard import dashboard_activity
    launch(world)
    grant = world.claim()
    item = world.ledger(UUID(grant['assignment_id']))[0]
    item = change(world.conn, 'obligation', item['id'], description='Prove **the helper** and link its evidence.')
    for n in range(8):
        world.command('comment_obligation', get(world.conn, 'obligation', item['id']), note=f'Comment **{n}**')
    response = dashboard_activity.session_detail(world.conn, world.actor, UUID(grant['assignment_id']),
        store=world.service.store, service=world.service, view='reports')
    ledger = response['reports'][0]['ledger']
    assert ledger['mission'] and ledger['acceptance_criteria']
    row = next(r for r in ledger['items'] if r['id'] == item['id'])
    assert '**the helper**' in row['description']
    assert len(row['comments']) == 5
    assert row['comments'][-1]['markdown'] == 'Comment **7**'
