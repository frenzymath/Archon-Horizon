from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select

from archon_horizon.pipeline import models
from archon_horizon.pipeline.records import change, create, get, snapshot
from archon_horizon.pipeline.schema import tables
from test_pipeline_service import service_database, world  # noqa: F401


def review_frontier(world):
    run = world.run(orchestrated=True)
    world.disable_automations(run)
    automation = tables['automation']
    rule = dict(world.conn.execute(select(automation).where(
        automation.c.run_id == run['id'], automation.c.name == 'maintainer')).mappings().one())
    rule = change(world.conn, 'automation', rule['id'], enabled=True)
    repository = get(world.conn, 'repository', world.document['source_repository_id'])
    create(world.conn, 'connector_cursor', integration_id=repository['integration_id'],
        consumer='forge:' + str(repository['id']), status='current', last_synced_at=datetime.now(timezone.utc))
    item = create(world.conn, 'forge_item', repository_id=repository['id'], origin_run_id=run['id'],
        review_phase='preprocessing', remote_number=1,
        kind='pull_request', title='Reviewed contract', status='open', head_commit_oid='a' * 40,
        target_branch='main', observed_at=datetime.now(timezone.utc))
    reviewer = world.assignment(run, functions=['reviewer'])
    descriptor = create(world.conn, 'reviewer_descriptor', project_id=world.project['id'],
        slug='contract-review', functions=['reviewer'], instructions='Inspect the contract', invocation='assignment')
    world.conn.execute(insert(tables['review_work']).values(forge_item_id=item['id'],
        head_commit_oid=item['head_commit_oid'], target_branch='main', assignment_id=reviewer['id'],
        reviewer_descriptor_revision_id=snapshot(world.conn, 'reviewer_descriptor', descriptor, world.actor.id),
        policy_revision_id=snapshot(world.conn, 'review_policy', world.policy, world.actor.id),
        obligation_id=world.ledger(reviewer['id'])[0]['id']))
    return run, rule, item, reviewer


def integration_owner(world, run, reviewer, *, functions=None):
    parent = get(world.conn, 'mission', world.mission['id'])
    mission = world.service.mission(world.conn, world.actor, models.MissionCreate(
        project_id=world.project['id'], parent_id=parent['id'], expected_parent_revision=parent['revision'],
        title='Integrate the reviewed contract', objective='Consume the pinned reviewer and integrate this PR.',
        acceptance_criteria=['Publish the scoped integration outcome'], delegation_note='Own this PR integration.'))
    return world.service.assignment(world.conn, world.actor, models.AssignmentCreate(
        run_id=run['id'], mission_id=mission['id'], role='maintainer', functions=functions or [],
        instructions='Collect this reviewer and integrate its exact PR.',
        start_condition={'version': 1, 'expression': {'op': 'status_in',
            'target': {'kind': 'assignment', 'id': str(reviewer['id'])},
            'values': ['completed', 'failed', 'cancelled']}}))


@pytest.mark.parametrize('status', ['pending', 'running', 'stopping'])
@pytest.mark.parametrize('settled', ['completed', 'failed', 'cancelled'])
def test_explicit_integrator_reserves_its_pr_until_terminal(world, status, settled):
    run, rule, _, reviewer = review_frontier(world)
    owner = integration_owner(world, run, reviewer)
    change(world.conn, 'assignment', owner['id'], status=status)
    assert world.scheduler.replenish(world.conn, world.actor, rule) is None
    assert get(world.conn, 'automation', rule['id'])['enabled'] is True

    change(world.conn, 'assignment', owner['id'], status=settled)
    generic = world.scheduler.replenish(world.conn, world.actor, rule)
    assert generic is not None and generic['automation_id'] == rule['id']


def test_unrelated_pr_in_same_repository_still_admits_generic_maintenance(world):
    run, rule, item, reviewer = review_frontier(world)
    integration_owner(world, run, reviewer)
    create(world.conn, 'forge_item', repository_id=item['repository_id'], origin_run_id=run['id'],
        review_phase='preprocessing', remote_number=2,
        kind='pull_request', title='Independent contribution', status='open', head_commit_oid='b' * 40,
        target_branch='main', observed_at=datetime.now(timezone.utc))
    generic = world.scheduler.replenish(world.conn, world.actor, rule)
    assert generic is not None
    assert world.service.readiness(world.conn, generic, datetime.now(timezone.utc)).ready


def test_preexisting_generic_waits_but_explicit_root_closure_keeps_its_own_condition(world):
    run, rule, _, reviewer = review_frontier(world)
    generic = world.scheduler.replenish(world.conn, world.actor, rule)
    owner = integration_owner(world, run, reviewer)
    ready = world.service.readiness(world.conn, generic, datetime.now(timezone.utc))
    assert not ready.ready and 'explicit integration owners' in ready.reason

    explicit = change(world.conn, 'assignment', generic['id'],
        instructions='After the scoped integrator finishes, reconcile historical ledgers and close the root mission.',
        start_condition={'version': 1, 'expression': {'op': 'status_in',
            'target': {'kind': 'assignment', 'id': str(owner['id'])},
            'values': ['completed', 'failed', 'cancelled']}})
    ready = world.service.readiness(world.conn, explicit, datetime.now(timezone.utc))
    assert not ready.ready and 'explicit integration owners' not in ready.reason
    change(world.conn, 'assignment', owner['id'], status='completed')
    assert world.service.readiness(world.conn, explicit, datetime.now(timezone.utc)).ready


@pytest.mark.parametrize('functions', [['orchestrator'], ['planner']])
def test_supervisory_profiles_do_not_reserve_mathematical_integration(world, functions):
    run, rule, _, reviewer = review_frontier(world)
    integration_owner(world, run, reviewer, functions=functions)
    assert world.scheduler.replenish(world.conn, world.actor, rule) is not None


def test_explicit_maintainer_assignment_is_not_blocked_by_generic_recurrence_guard(world):
    run, rule, _, reviewer = review_frontier(world)
    integration_owner(world, run, reviewer)
    explicit = world.assignment(run, role='maintainer', instructions=rule['instructions'],
        start_condition=rule['start_condition'])
    assert world.service.readiness(world.conn, explicit, datetime.now(timezone.utc)).ready


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('wait_kind', ['forge_revision', 'reviewer_revision', 'negated_reviewer',
                                      'mixed_wait', 'unrelated_assignment'])
def test_waiting_for_a_producer_does_not_reserve_its_pr(world, existing, wait_kind):
    run, rule, item, reviewer = review_frontier(world)
    generic = world.scheduler.replenish(world.conn, world.actor, rule) if existing else None
    owner = integration_owner(world, run, reviewer)
    forge_revision = {'op': 'revision_after', 'target': {'kind': 'forge_item', 'id': str(item['id'])},
                      'revision': item['revision']}
    terminal_review = owner['start_condition']['expression']
    other = world.assignment(run)
    expression = {
        'forge_revision': forge_revision,
        'reviewer_revision': {'op': 'revision_after',
            'target': {'kind': 'assignment', 'id': str(reviewer['id'])}, 'revision': reviewer['revision']},
        'negated_reviewer': {'op': 'not', 'arg': terminal_review},
        'mixed_wait': {'op': 'all', 'args': [terminal_review, forge_revision]},
        'unrelated_assignment': {'op': 'all', 'args': [terminal_review,
            {'op': 'status_in', 'target': {'kind': 'assignment', 'id': str(other['id'])},
             'values': ['completed', 'failed', 'cancelled']}]},
    }[wait_kind]
    condition = models.Condition.model_validate({'version': 1, 'expression': expression}).model_dump(mode='json')
    world.service.validate_condition(world.conn, world.project['id'], condition, owner['id'])
    change(world.conn, 'assignment', owner['id'], start_condition=condition,
           instructions='Consume the next producer result, then publish the final index.')

    if not existing:
        generic = world.scheduler.replenish(world.conn, world.actor, rule)
    assert generic is not None
    assert world.service.readiness(world.conn, generic, datetime.now(timezone.utc)).ready


def test_review_handoff_can_include_a_terminal_trusted_job():
    from uuid import uuid4
    from archon_horizon.pipeline.review_backlog import _terminal_review_dependencies
    reviewer_id = uuid4()
    condition = models.Condition.model_validate({'expression': {'op': 'all', 'args': [
        {'op': 'status_in', 'target': {'kind': 'assignment', 'id': str(reviewer_id)},
         'values': ['completed', 'failed', 'cancelled']},
        {'op': 'status_in', 'target': {'kind': 'milestone_job', 'id': str(uuid4())},
         'values': ['completed', 'failed']},
    ]}}).model_dump(mode='json')
    assert _terminal_review_dependencies(condition) == {reviewer_id}
