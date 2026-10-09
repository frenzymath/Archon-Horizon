from uuid import UUID

import pytest

from archon_horizon.pipeline import models
from archon_horizon.pipeline.auth import authenticate
from archon_horizon.pipeline.persistence.records import change, create, get
from test_pipeline_service import service_database, world  # noqa: F401


def test_planner_initial_ledger_owns_a_decision_without_replacing_productive_deliverables(world):
    run = world.run()
    world.disable_automations(run)
    planner = world.assignment(run, functions=['planner'])
    root = world.ledger(planner['id'])[0]
    assert root['kind'] == 'decision'
    assert root['description'] == (
        'Account for one bounded planning pass: identify concrete unowned work '
        'or preserve existing owners and their wake conditions.')
    assert root['status'] == 'open' and root['resolution'] is None
    for role in ('worker', 'maintainer'):
        owner = world.assignment(run, role=role)
        deliverable = world.ledger(owner['id'])[0]
        assert deliverable['kind'] == 'deliverable'
        assert deliverable['description'] == world.mission['objective']


@pytest.mark.parametrize('legacy_root', [False, True])
def test_planner_must_explicitly_account_for_its_ledger_before_finishing(world, legacy_root):
    run = world.run()
    world.disable_automations(run)
    planner = world.assignment(run, functions=['planner'])
    root = world.ledger(planner['id'])[0]
    if legacy_root:
        root = change(world.conn, 'obligation', root['id'],
                      kind='deliverable', description=world.mission['objective'])
    claim = world.claim()
    assert UUID(claim['assignment_id']) == planner['id']
    create(world.conn, 'provider_request', execution_id=UUID(claim['execution_id']),
        provider_thread_id=UUID(claim['provider_thread_record_id']), number=1,
        reason='assignment', status='completed')
    assert any('1 open obligations' in finding
               for finding in world.service.completion_findings(world.conn, planner['id']))
    unfinished = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, 'execution', claim['execution_id']), 'succeeded')
    assert unfinished['status'] == 'pending'
    assert get(world.conn, 'obligation', root['id']) == root
    if legacy_root:
        # The historical mathematical deliverable still needs its own accounting;
        # a completed planning pass must not rewrite or discharge it.
        return

    change(world.conn, 'assignment', planner['id'], not_before=None)
    resumed = world.claim()
    assert UUID(resumed['assignment_id']) == planner['id']
    actor = authenticate(world.conn, resumed['execution_token'])
    existing_owner = world.assignment(run, instructions='Prepare the bounded milestone contracts.')
    resolved = world.service.resolve_obligation(world.conn, actor, root['id'], models.ObligationResolve(
        expected_revision=root['revision'], status='done', resolution={
            'kind': 'completed', 'note': 'The existing queued worker owns the ready contract work; no duplicate dispatch is needed.',
            'evidence': [{'kind': 'assignment', 'id': str(existing_owner['id'])}]}))
    create(world.conn, 'provider_request', execution_id=UUID(resumed['execution_id']),
        provider_thread_id=UUID(resumed['provider_thread_record_id']), number=2,
        reason='continuation', status='completed')
    assert world.service.completion_findings(world.conn, planner['id']) == []
    completed = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, 'execution', resumed['execution_id']), 'succeeded')
    assert completed['status'] == 'completed'
    assert get(world.conn, 'obligation', root['id']) == resolved
    assert get(world.conn, 'mission', world.mission['id'])['status'] == 'open'
    assert world.ledger(existing_owner['id'])[0]['status'] == 'open'
