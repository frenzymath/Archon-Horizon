from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.execution.worker_events import WorkerOperation, handle
from test_pipeline_service import service_database, world  # noqa: F401


def admitted_without_native(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    request = create(world.conn, 'provider_request', provider_thread_id=UUID(claim['provider_thread_record_id']),
        execution_id=UUID(claim['execution_id']), number=1, reason='assignment', status='submitted')
    return assignment, claim, request


def stop(world, claim, reason):
    return handle(world.conn, world.host_actor, WorkerOperation(operation_id=uuid4(),
        execution_id=claim['execution_id'], epoch=claim['epoch'], kind='execution_finished',
        payload={'status': 'yielded', 'reason': reason, 'provider_thread_id': None},
        occurred_at=datetime.now(timezone.utc).timestamp()), world.service, world.scheduler)


def test_unacknowledged_admission_without_provider_launch_retries_same_context(world):
    assignment, claim, request = admitted_without_native(world)
    stop(world, claim, 'request_admission_not_acknowledged')
    assert get(world.conn, 'provider_request', request['id'])['status'] == 'interrupted'
    thread = get(world.conn, 'provider_thread', claim['provider_thread_record_id'])
    assert thread['status'] == 'creating' and thread['provider_thread_id'] is None
    pending = get(world.conn, 'assignment', assignment['id'])
    world.command('update_assignment', pending, not_before=None)
    resumed = world.claim()
    assert resumed['assignment_id'] == claim['assignment_id']
    assert resumed['provider_thread_record_id'] == claim['provider_thread_record_id']
    assert resumed['provider_thread_id'] is None


def test_prelaunch_episode_budget_preserves_context_for_explicit_retry(world):
    assignment, claim, request = admitted_without_native(world)
    stopped = stop(world, claim, 'execution_budget_reached')
    assert stopped['status'] == 'failed'
    assert get(world.conn, 'provider_request', request['id'])['status'] == 'interrupted'
    thread = get(world.conn, 'provider_thread', claim['provider_thread_record_id'])
    assert thread['status'] == 'creating' and thread['provider_thread_id'] is None

    world.command('retry_assignment', get(world.conn, 'assignment', assignment['id']))
    resumed = world.claim()
    assert resumed['assignment_id'] == claim['assignment_id']
    assert resumed['provider_thread_record_id'] == claim['provider_thread_record_id']
    assert resumed['provider_thread_id'] is None


@pytest.mark.parametrize('reason', ['request_admission_not_acknowledged', 'execution_budget_reached'])
@pytest.mark.parametrize('evidence', ['started', 'completed', 'native_turn', 'ambiguous_reason'])
def test_prelaunch_recovery_does_not_discard_evidence_of_possible_native_work(world, evidence, reason):
    assignment, claim, request = admitted_without_native(world)
    if evidence == 'started':
        change(world.conn, 'provider_request', request['id'], started_at=datetime.now(timezone.utc))
    elif evidence == 'completed':
        change(world.conn, 'provider_request', request['id'], status='completed')
    elif evidence == 'native_turn':
        change(world.conn, 'provider_request', request['id'], provider_turn_id='native-turn')
    else:
        reason = 'control_plane_unavailable_at_boundary'
    stop(world, claim, reason)
    assert get(world.conn, 'provider_thread', claim['provider_thread_record_id'])['status'] == 'unavailable'


def test_request_count_budget_is_not_proof_of_an_unstarted_context(world):
    assignment, claim, request = admitted_without_native(world)
    stop(world, claim, 'request_budget_reached')
    assert get(world.conn, 'provider_thread', claim['provider_thread_record_id'])['status'] == 'unavailable'


@pytest.mark.parametrize('reason', ['request_admission_not_acknowledged', 'execution_budget_reached'])
def test_prelaunch_abort_preserves_known_native_identity(world, reason):
    assignment, claim, request = admitted_without_native(world)
    change(world.conn, 'provider_thread', claim['provider_thread_record_id'],
           provider_thread_id='retained-native', status='available')
    stop(world, claim, reason)
    thread = get(world.conn, 'provider_thread', claim['provider_thread_record_id'])
    assert thread['status'] == 'available' and thread['provider_thread_id'] == 'retained-native'
