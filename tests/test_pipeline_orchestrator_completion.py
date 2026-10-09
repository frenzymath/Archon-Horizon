from uuid import UUID

import pytest
from sqlalchemy import select

from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import service_database, world  # noqa: F401


def claim_orchestrator(world):
    run = world.run(orchestrated=True)
    claim = world.claim()
    assignment = get(world.conn, 'assignment', claim['assignment_id'])
    assert assignment['functions'] == ['orchestrator']
    create(world.conn, 'provider_request', execution_id=UUID(claim['execution_id']),
        provider_thread_id=UUID(claim['provider_thread_record_id']), number=1,
        reason='assignment', status='completed')
    return run, claim, assignment


@pytest.mark.parametrize('legacy_root', [False, True])
def test_completed_orchestrator_settles_only_its_episode_without_a_successor(world, legacy_root):
    run, claim, assignment = claim_orchestrator(world)
    root = world.ledger(assignment['id'])[0]
    assert root['kind'] == 'decision'
    assert root['description'] != world.mission['objective']
    if legacy_root:
        change(world.conn, 'obligation', root['id'], kind='deliverable', description=world.mission['objective'])
    change(world.conn, 'automation', assignment['automation_id'], enabled=False)

    completed = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, 'execution', claim['execution_id']), 'succeeded')
    assert completed['status'] == 'completed'
    root = get(world.conn, 'obligation', root['id'])
    assert root['status'] == 'done'
    assert 'does not complete or accept the mathematical mission' in root['resolution']['note']
    assert get(world.conn, 'mission', world.mission['id'])['status'] == 'open'
    assert len(list(world.conn.execute(select(tables['assignment']).where(
        tables['assignment'].c.run_id == run['id'])).mappings())) == 1

    world.command('complete_mission', get(world.conn, 'mission', world.mission['id']),
                  note='Independent mathematical acceptance is recorded by the maintainer.')
    draining = world.command('drain_run', get(world.conn, 'run', run['id']))
    assert world.command('complete_run', draining)['status'] == 'completed'


def test_orchestrator_finishing_during_drain_does_not_strand_its_episode_ledger(world):
    run, claim, assignment = claim_orchestrator(world)
    world.command('complete_mission', get(world.conn, 'mission', world.mission['id']),
                  note='Independent mathematical acceptance is recorded by the maintainer.')
    draining = world.command('drain_run', get(world.conn, 'run', run['id']))
    completed = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, 'execution', claim['execution_id']), 'succeeded')
    assert completed['status'] == 'completed'
    assert world.ledger(assignment['id'])[0]['status'] == 'done'
    assert world.command('complete_run', draining)['status'] == 'completed'


def test_orchestrator_cannot_hide_an_explicit_unresolved_control_obligation(world):
    run, claim, assignment = claim_orchestrator(world)
    control = create(world.conn, 'obligation', assignment_id=assignment['id'], number=2,
        created_by_execution_id=UUID(claim['execution_id']), kind='decision',
        description='Reconcile the failed scheduling mutation with its authoritative outcome.')
    findings = world.service.completion_findings(world.conn, assignment['id'])
    assert any('1 open obligations' in finding for finding in findings)
    completed = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, 'execution', claim['execution_id']), 'succeeded')
    assert completed['status'] == 'failed'
    assert get(world.conn, 'obligation', control['id'])['status'] == 'open'
    assert get(world.conn, 'mission', world.mission['id'])['status'] == 'open'
    world.command('complete_mission', get(world.conn, 'mission', world.mission['id']),
                  note='The mathematical work was separately accepted.')
    draining = world.command('drain_run', get(world.conn, 'run', run['id']))
    with pytest.raises(DomainError) as error:
        world.command('complete_run', draining)
    assert error.value.code == 'run_unsettled'
