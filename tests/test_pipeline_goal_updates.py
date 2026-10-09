from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from archon_horizon.pipeline import models
from archon_horizon.pipeline.auth import authenticate
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.persistence.records import change, create, get, save_blob, snapshot
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.execution.worker_events import WorkerOperation, handle
from test_pipeline_service import service_database, world  # noqa: F401


def revised_goal(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    mission = world.service.update_mission(world.conn, world.actor, world.mission['id'], models.MissionUpdate(
        expected_revision=world.mission['revision'], objective='Prove the clarified theorem'))
    completed = world.command('complete_mission', mission, note='The clarified theorem and publication are complete')
    applied_revision = snapshot(world.conn, 'mission', completed, world.actor.id)
    operation = dict(world.conn.execute(select(tables['outbox_operation']).where(
        tables['outbox_operation'].c.kind == 'goal_update')).mappings().one())
    return run, assignment, claim, applied_revision, operation


def test_newer_acknowledged_mission_settles_old_goal_without_terminal_assignment(world):
    run, assignment, claim, applied, operation = revised_goal(world)
    change(world.conn, 'provider_thread', claim['provider_thread_record_id'],
           applied_mission_revision_id=applied, applied_run_revision=run['revision'])
    assert world.service.reconcile_goal_updates(world.conn) == 1
    settled = get(world.conn, 'outbox_operation', operation['id'])
    assert settled['status'] == 'completed'
    assert settled['revision'] == operation['revision'] + 1
    assert world.service.reconcile_goal_updates(world.conn) == 0
    assert get(world.conn, 'assignment', assignment['id'])['status'] == 'running'
    assert not any('updated mission' in item for item in world.service.completion_findings(world.conn, assignment['id']))


@pytest.mark.parametrize('mismatch', ['unacknowledged', 'older_mission', 'other_mission', 'older_run',
                                     'different_roadmap', 'running', 'uncertain'])
def test_goal_reconciliation_requires_matching_consumed_inputs_and_unsent_operation(world, mismatch):
    run, assignment, claim, applied, operation = revised_goal(world)
    values = {'applied_mission_revision_id': applied, 'applied_run_revision': run['revision']}
    if mismatch == 'unacknowledged':
        values['applied_mission_revision_id'] = None
    elif mismatch == 'older_mission':
        values['applied_mission_revision_id'] = snapshot(world.conn, 'mission', world.mission, world.actor.id)
    elif mismatch == 'other_mission':
        other = world.service.mission(world.conn, world.actor, models.MissionCreate(
            project_id=world.project['id'], title='Unrelated mission', objective='Prove another statement'))
        values['applied_mission_revision_id'] = snapshot(world.conn, 'mission', other, world.actor.id)
    elif mismatch == 'older_run':
        values['applied_run_revision'] = run['revision'] - 1
    elif mismatch == 'different_roadmap':
        change(world.conn, 'outbox_operation', operation['id'],
               payload={**operation['payload'], 'roadmap_snapshot_id': str(uuid4())})
    elif mismatch in ('running', 'uncertain'):
        change(world.conn, 'outbox_operation', operation['id'], status=mismatch)
    change(world.conn, 'provider_thread', claim['provider_thread_record_id'], **values)
    assert world.service.reconcile_goal_updates(world.conn) == 0
    assert get(world.conn, 'outbox_operation', operation['id'])['status'] == (
        mismatch if mismatch in ('running', 'uncertain') else 'pending')


def test_worker_completion_acknowledges_newer_goal_and_settles_superseded_update(world):
    run, assignment, claim, applied, operation = revised_goal(world)
    inputs = save_blob(world.conn, world.service.store, world.project['id'], {
        'prompt': 'Continue retained context', 'mission_revision_id': str(applied),
        'run_revision': run['revision'], 'roadmap_snapshot_id': None})
    request = create(world.conn, 'provider_request', provider_thread_id=UUID(claim['provider_thread_record_id']),
        execution_id=UUID(claim['execution_id']), number=1, reason='continuation', status='submitted',
        input_artifact_id=inputs['id'])
    handle(world.conn, world.host_actor, WorkerOperation(operation_id=uuid4(),
        execution_id=claim['execution_id'], epoch=claim['epoch'], kind='provider_observed',
        occurred_at=datetime.now(timezone.utc).timestamp(), payload={
            'event': 'request_completed', 'provider_thread_record_id': claim['provider_thread_record_id'],
            'request_id': str(request['id']), 'status': 'completed'}), world.service, world.scheduler)
    assert get(world.conn, 'provider_thread', claim['provider_thread_record_id'])['applied_mission_revision_id'] == applied
    assert get(world.conn, 'outbox_operation', operation['id'])['status'] == 'completed'


def test_admission_poll_recovers_old_consumed_goal_update(world):
    run, assignment, claim, applied, operation = revised_goal(world)
    change(world.conn, 'provider_thread', claim['provider_thread_record_id'],
           applied_mission_revision_id=applied, applied_run_revision=run['revision'])
    world.claim()
    assert get(world.conn, 'outbox_operation', operation['id'])['status'] == 'completed'


def test_noop_mission_patch_preserves_revision_and_does_not_generate_goal_updates(world):
    run = world.run()
    world.disable_automations(run)
    world.assignment(run)
    claim = world.claim()
    actor = authenticate(world.conn, claim['execution_token'])
    original = get(world.conn, 'mission', world.mission['id'])
    updated = world.service.update_mission(world.conn, actor, original['id'], models.MissionUpdate(
        expected_revision=original['revision'], objective=original['objective'],
        delegation_note=original['delegation_note']))
    assert updated == original
    assert not world.conn.execute(select(tables['outbox_operation'].c.id).where(
        tables['outbox_operation'].c.kind == 'goal_update')).first()
    with pytest.raises(DomainError, match='record changed'):
        world.service.update_mission(world.conn, actor, original['id'], models.MissionUpdate(
            expected_revision=original['revision'] + 1, objective=original['objective']))


def test_continuation_consumes_changed_goal_without_an_acknowledgment_patch(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, functions=['planner'])
    claim = world.claim()
    actor = authenticate(world.conn, claim['execution_token'])
    for obligation in world.ledger(assignment['id']):
        world.service.resolve_obligation(world.conn, actor, obligation['id'], models.ObligationResolve(
            expected_revision=obligation['revision'], status='done',
            resolution={'kind': 'completed', 'note': 'Bounded planning and future ownership recorded.'}))
    revised = world.service.update_mission(world.conn, world.actor, world.mission['id'], models.MissionUpdate(
        expected_revision=world.mission['revision'], acceptance_criteria=['Prepare the reviewed baseline packet']))
    create(world.conn, 'provider_request', provider_thread_id=UUID(claim['provider_thread_record_id']),
        execution_id=UUID(claim['execution_id']), number=1, reason='assignment', status='completed')
    first = world.scheduler.heartbeat(world.conn, world.host_actor, UUID(claim['execution_id']), claim['epoch'])
    assert first['continue'] is True
    assert first['mission_revision_number'] == revised['revision']
    assert 'automatically acknowledges' in first['goal']
    assert 'Do not PATCH the mission' in first['goal']
    inputs = save_blob(world.conn, world.service.store, world.project['id'], {
        'prompt': first['goal'], 'mission_revision_id': first['mission_revision_id'],
        'run_revision': first['run_revision'], 'roadmap_snapshot_id': first['roadmap_snapshot_id']})
    request = create(world.conn, 'provider_request', provider_thread_id=UUID(claim['provider_thread_record_id']),
        execution_id=UUID(claim['execution_id']), number=2, reason='continuation', status='submitted',
        input_artifact_id=inputs['id'])
    handle(world.conn, world.host_actor, WorkerOperation(operation_id=uuid4(),
        execution_id=claim['execution_id'], epoch=claim['epoch'], kind='provider_observed',
        occurred_at=datetime.now(timezone.utc).timestamp(), payload={
            'event': 'request_completed', 'provider_thread_record_id': claim['provider_thread_record_id'],
            'request_id': str(request['id']), 'status': 'completed'}), world.service, world.scheduler)
    assert get(world.conn, 'mission', revised['id'])['revision'] == revised['revision']
    assert get(world.conn, 'provider_thread', claim['provider_thread_record_id'])['applied_mission_revision_id'] == UUID(first['mission_revision_id'])
    assert world.service.completion_findings(world.conn, assignment['id']) == []
    assert world.scheduler.heartbeat(world.conn, world.host_actor, UUID(claim['execution_id']), claim['epoch'])['continue'] is False


@pytest.mark.parametrize('values', [{'objective': 'A genuinely changed result'},
    {'acceptance_criteria': ['A stronger required check']}, {'delegation_note': 'New guidance from this execution'}])
def test_real_self_edits_still_require_consumption_of_the_new_goal(world, values):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    actor = authenticate(world.conn, claim['execution_token'])
    current = get(world.conn, 'mission', world.mission['id'])
    revised = world.service.update_mission(world.conn, actor, current['id'], models.MissionUpdate(
        expected_revision=current['revision'], **values))
    assert revised['revision'] == current['revision'] + 1
    operation = world.conn.execute(select(tables['outbox_operation']).where(
        tables['outbox_operation'].c.kind == 'goal_update')).mappings().one()
    assert operation['status'] == 'pending'
    assert get(world.conn, 'provider_thread', claim['provider_thread_record_id'])['applied_mission_revision_id'] is None
    assert any('updated mission' in finding for finding in world.service.completion_findings(world.conn, assignment['id']))
