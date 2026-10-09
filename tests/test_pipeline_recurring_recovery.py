from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from archon_horizon.pipeline import models
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.persistence.records import change, get
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import service_database, world  # noqa: F401


def failed_occurrence(world):
    run = world.run()
    claim = world.claim()
    item = get(world.conn, 'assignment', claim['assignment_id'])
    assert item['automation_id']
    failed = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, 'execution', claim['execution_id']), 'failed')
    assert failed['status'] == 'failed'
    assert not get(world.conn, 'automation', item['automation_id'])['enabled']
    return run, failed, claim


@pytest.mark.parametrize('operation', ['retry_assignment', 'resume_assignment'])
def test_explicit_recovery_reenables_failure_disabled_rule(world, operation):
    run, failed, claim = failed_occurrence(world)
    automation = get(world.conn, 'automation', failed['automation_id'])
    args = {'note': 'The host issue is repaired; continue retained work'} if operation == 'resume_assignment' else {}
    resumed = world.command(operation, failed, **args)
    restored = get(world.conn, 'automation', automation['id'])
    assert restored['enabled'] and restored['revision'] == automation['revision'] + 1
    assert restored['start_condition'] == automation['start_condition']
    assert restored['not_before'] == automation['not_before']
    assert world.service.readiness(world.conn, resumed, datetime.now(timezone.utc)).ready
    next_claim = world.claim()
    assert next_claim['assignment_id'] == claim['assignment_id']
    assert next_claim['provider_thread_record_id'] == claim['provider_thread_record_id']
    event, reference = tables['event'], tables['object_reference']
    notes = list(world.conn.execute(select(event.c.payload).join(reference, event.c.subject_id == reference.c.id).where(
        reference.c.automation_id == automation['id'])).scalars())
    assert any('re-enabled' in (row.get('note') or '') for row in notes)


@pytest.mark.parametrize('operation', ['retry_assignment', 'resume_assignment'])
def test_explicit_recovery_rejects_an_existing_active_successor(world, operation):
    run, failed, claim = failed_occurrence(world)
    successor = world.service.assignment(world.conn, world.actor,
        models.AssignmentCreate(run_id=run['id'], mission_id=failed['mission_id']),
        automation_id=failed['automation_id'], internal=True)
    args = {'note': 'Continue previous context'} if operation == 'resume_assignment' else {}
    with pytest.raises(DomainError) as error:
        world.command(operation, failed, **args)
    assert error.value.code == 'active_successor'
    assert not get(world.conn, 'automation', failed['automation_id'])['enabled']
    assert get(world.conn, 'assignment', failed['id'])['status'] == 'failed'
    assert get(world.conn, 'assignment', successor['id'])['status'] == 'pending'


def test_retry_does_not_reenable_a_cancelled_run(world):
    run, failed, claim = failed_occurrence(world)
    world.command('cancel_run', get(world.conn, 'run', run['id']), note='Operator cancelled the campaign')
    with pytest.raises(DomainError) as error:
        world.command('retry_assignment', failed)
    assert error.value.code == 'run_not_active'
    assert not get(world.conn, 'automation', failed['automation_id'])['enabled']


def test_completed_recurring_context_can_resume_after_successor_is_explicitly_cancelled(world):
    run = world.run()
    claim = world.claim()
    assignment = get(world.conn, 'assignment', claim['assignment_id'])
    for obligation in world.ledger(assignment['id']):
        change(world.conn, 'obligation', obligation['id'], status='done',
               resolution={'kind': 'completed', 'note': 'This occurrence finished', 'evidence': []})
    completed = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, 'execution', claim['execution_id']), 'succeeded')
    assert completed['status'] == 'completed'
    successor = dict(world.conn.execute(select(tables['assignment']).where(
        tables['assignment'].c.automation_id == assignment['automation_id'],
        tables['assignment'].c.status == 'pending')).mappings().one())
    world.command('cancel_assignment', successor, note='Continue the original retained context instead')
    assert not get(world.conn, 'automation', assignment['automation_id'])['enabled']
    resumed = world.command('resume_assignment', completed, note='Address newly identified review work')
    assert get(world.conn, 'automation', assignment['automation_id'])['enabled']
    assert world.service.readiness(world.conn, resumed, datetime.now(timezone.utc)).ready
    next_claim = world.claim()
    assert next_claim['provider_thread_record_id'] == claim['provider_thread_record_id']


def test_automation_enable_toggle_preserves_pending_occurrence_schedule(world):
    run = world.run()
    assignment = dict(world.conn.execute(select(tables['assignment']).where(
        tables['assignment'].c.run_id == run['id'], tables['assignment'].c.functions.contains(['planner']))).mappings().one())
    condition = {'version': 1, 'expression': {'op': 'status_in',
        'target': {'kind': 'mission', 'id': str(world.mission['id'])}, 'values': ['completed']}}
    waiting = world.command('update_assignment', assignment, start_condition=condition,
                            not_before=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    automation = get(world.conn, 'automation', assignment['automation_id'])
    disabled = world.command('defer_automation', automation, enabled=False)
    assert get(world.conn, 'assignment', assignment['id']) == waiting
    assert not world.service.readiness(world.conn, waiting, datetime.now(timezone.utc)).ready
    world.command('defer_automation', disabled, enabled=True)
    assert get(world.conn, 'assignment', assignment['id']) == waiting


def test_enabling_successor_rule_does_not_validate_it_as_current_occurrence_condition(world):
    run = world.run()
    assignment = dict(world.conn.execute(select(tables['assignment']).where(
        tables['assignment'].c.run_id == run['id'], tables['assignment'].c.functions.contains(['planner']))).mappings().one())
    successor_condition = {'version': 1, 'expression': {'op': 'status_in',
        'target': {'kind': 'assignment', 'id': str(assignment['id'])}, 'values': ['completed']}}
    # The next occurrence legitimately waits for this one; its ready condition is separate.
    automation = change(world.conn, 'automation', assignment['automation_id'],
                        enabled=False, start_condition=successor_condition)
    enabled = world.command('defer_automation', automation, enabled=True)
    assert enabled['start_condition'] == successor_condition
    assert get(world.conn, 'assignment', assignment['id']) == assignment
    assert world.service.readiness(world.conn, assignment, datetime.now(timezone.utc)).ready
    with pytest.raises(DomainError):
        world.command('defer_automation', enabled, start_condition=successor_condition)
