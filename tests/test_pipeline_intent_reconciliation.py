from datetime import datetime, timezone
import json
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import insert, select

from archon_horizon.pipeline import models
from archon_horizon.pipeline.client import AgentClient
from archon_horizon.pipeline.execution.intent_reconciliation import JOURNAL_BLOCKER_PREFIX, INTENT_REPAIR_PREFIX, blocker_id
from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.execution.worker_events import WorkerOperation, handle
from test_pipeline_api import api, api_database  # noqa: F401
from test_pipeline_service import service_database, world  # noqa: F401
from test_pipeline_worker import journal, repository  # noqa: F401


@pytest.mark.parametrize('status,code', [(404, 'not_found'), (422, 'delegator_required'),
    (409, 'dependency_cycle'), (409, 'review_gate_blocked'),
    (409, 'incomplete_initial_discussion_read'), (409, 'unread_messages'),
    (422, 'self_delegation'), (422, 'unavailable_owner'), (409, 'open_children'),
    (409, 'child_budget_exhausted'), (422, 'review_resolutions_required'), (422, 'review_plan_incomplete'),
    (422, 'review_resolution_independence'), (422, 'review_resolution_invalid')])
def test_definite_rejections_retain_diagnostics_without_recovery_loop(tmp_path, status, code):
    body = {'error': {'code': code, 'message': 'Request did not run'}}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))) as transport:
        client = AgentClient('https://horizon.invalid', 'token', 'old', tmp_path, client=transport)
        with pytest.raises(RuntimeError):
            client.request('POST', '/api/v3/commands', {'bad': 'reference'})
        with client.connect() as db:
            row = db.execute('SELECT id,status,response FROM intent').fetchone()
            assert row['status'] == 'invalid'
            assert json.loads(row['response']) == body
            # The old daemon retained exactly this rejection as a recovery blocker.
            db.execute("UPDATE intent SET status='rejected' WHERE id=?", (row['id'],))
        resumed = AgentClient('https://horizon.invalid', 'fresh', 'new', tmp_path, client=transport)
        for _ in range(610):
            assert resumed.replay_pending() == {'completed': [], 'blocked': [], 'pending': 0}
        with resumed.connect() as db:
            retained = db.execute('SELECT status,response FROM intent').fetchone()
            assert retained['status'] == 'invalid' and json.loads(retained['response']) == body


@pytest.mark.parametrize('status,body', [
    (409, {'error': {'code': 'revision_conflict'}}),
    (409, {'error': {'code': 'idempotency_conflict'}}),
    (404, {'error': {'code': 'unknown_delivery_state'}}),
    (401, {'error': {'code': 'not_authenticated'}}),
    (503, {'error': {'code': 'database_unavailable'}}),
])
def test_conflicting_and_uncertain_mutations_stay_recoverable(tmp_path, status, body):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))) as transport:
        client = AgentClient('https://horizon.invalid', 'token', 'first', tmp_path, client=transport)
        with pytest.raises(RuntimeError):
            client.request('POST', '/api/v3/commands', {'operation': 'work'})
        pending = client.pending()
        assert len(pending) == 1
        assert f'agent resolve-intent {pending[0]["id"]} --note' in pending[0]['recovery_command']
        first = client.recovery_state()
        assert first['pending'] == 1 and first['consecutive_observations'] == 1
        assert client.recovery_state() == first
        resumed = AgentClient('https://horizon.invalid', 'fresh', 'second', tmp_path, client=transport)
        second = resumed.recovery_state()
        assert second['fingerprint'] == first['fingerprint'] and second['consecutive_observations'] == 2


@pytest.mark.parametrize('code', ['review_resolutions_required', 'review_plan_incomplete',
    'review_resolution_independence', 'review_resolution_invalid'])
def test_corrected_reviewer_report_leaves_only_rejection_diagnostics(tmp_path, code):
    rejection = {'error': {'code': code, 'message': 'Complete the approval assessment before publication'}}
    bodies = []

    def response(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(422, json=rejection)
        return httpx.Response(200, json={'idempotency_key': request.headers['Idempotency-Key']})

    corrected = {'verdict': 'approved', 'assessment': {
        'scope': 'The repaired contract at the pinned head', 'dimensions': ['mathematical_contract'],
        'complete': True, 'classification_confirmed': True, 'evidence': ['Current-head contract inspection'],
        'resolutions': [{'review_id': str(uuid4()), 'disposition': 'repaired',
                         'explanation': 'The requested hypothesis repair is present',
                         'evidence': ['The current declaration has the corrected hypothesis']}]
            if code != 'review_plan_incomplete' else []}}
    path = f'/api/v3/reviewer-invocations/{uuid4()}/report'
    original = {'verdict': 'approved', 'summary': 'Initial approval'}
    if code in ('review_resolution_independence', 'review_resolution_invalid'):
        original = {**corrected, 'assessment': {**corrected['assessment'], 'resolutions': [
            {**corrected['assessment']['resolutions'][0], 'review_id': str(uuid4())}]}}
    if code == 'review_resolution_independence':
        path = '/api/v3/forge/review'
        pins = {'forge_item_id': str(uuid4()), 'commit_oid': 'a' * 40}
        original = {**original, **pins}
        corrected = {**pins, 'verdict': 'commented',
            'summary': 'The independent reviewers own correction of their prior findings; their invocations are queued.'}
    with httpx.Client(transport=httpx.MockTransport(response)) as transport:
        client = AgentClient('https://horizon.invalid', 'token', 'review-execution', tmp_path, client=transport)
        with pytest.raises(RuntimeError, match=code):
            client.request('POST', path, original)
        delivered = client.request('POST', path, corrected)
        assert client.replay_pending() == {'completed': [], 'blocked': [], 'pending': 0}
        with client.connect() as db:
            rows = db.execute('SELECT id,status,response FROM intent ORDER BY created_at').fetchall()
            assert len(rows) == 2
            assert rows[0]['status'] == 'invalid' and json.loads(rows[0]['response']) == rejection
            assert rows[1]['status'] == 'completed' and rows[1]['id'] == delivered['idempotency_key']
        assert bodies == [original, corrected]


def test_durable_intent_repair_preserves_diagnostics_and_resets_recovery(tmp_path):
    assignment, obligation = str(uuid4()), str(uuid4())
    body = {'error': {'code': 'revision_conflict', 'message': 'Original rejection'}}
    def response(request):
        if request.url.path == '/api/v3/commands':
            return httpx.Response(409, json=body)
        return httpx.Response(200, json={'id': obligation, 'revision': 1})
    with httpx.Client(transport=httpx.MockTransport(response)) as transport:
        client = AgentClient('https://horizon.invalid', 'token', 'first', tmp_path,
                             client=transport, assignment_id=assignment)
        with pytest.raises(RuntimeError):
            client.request('POST', '/api/v3/commands', {'operation': 'work'})
        key = client.pending()[0]['id']
        client.recovery_state()
        resumed = AgentClient('https://horizon.invalid', 'fresh', 'second', tmp_path,
                              client=transport, assignment_id=assignment)
        assert resumed.recovery_state()['consecutive_observations'] == 2
        resumed.resolve_intent(key, 'Read the authoritative revision; the corrected command completed.')
        assert resumed.recovery_state() == {'pending': 0, 'fingerprint': None,
            'consecutive_observations': 0, 'intents': []}
        with resumed.connect() as db:
            repaired = db.execute('SELECT status,response,resolution FROM intent WHERE id=?', (key,)).fetchone()
            assert repaired['status'] == 'resolved' and json.loads(repaired['response']) == body
            assert json.loads(repaired['resolution'])['obligation_id'] == obligation
        with pytest.raises(RuntimeError):
            resumed.request('POST', '/api/v3/commands', {'operation': 'different-work'})
        assert resumed.recovery_state()['consecutive_observations'] == 1


def test_interrupted_repair_replays_original_note_across_executions(tmp_path):
    assignment, obligation = str(uuid4()), str(uuid4())
    repaired = False
    dispositions = []
    def response(request):
        if request.url.path == '/api/v3/commands':
            return httpx.Response(409, json={'error': {'code': 'revision_conflict'}})
        if request.url.path.endswith('/resolve'):
            dispositions.append(json.loads(request.content))
            if not repaired:
                return httpx.Response(503, json={'error': {'code': 'database_unavailable'}})
        return httpx.Response(200, json={'id': obligation, 'revision': 1})
    with httpx.Client(transport=httpx.MockTransport(response)) as transport:
        first = AgentClient('https://horizon.invalid', 'token', 'first', tmp_path,
                            client=transport, assignment_id=assignment)
        with pytest.raises(RuntimeError):
            first.request('POST', '/api/v3/commands', {'operation': 'work'})
        key = first.pending()[0]['id']
        with pytest.raises(RuntimeError):
            first.resolve_intent(key, 'The corrected command completed at revision 2.')
        repaired = True
        resumed = AgentClient('https://horizon.invalid', 'fresh', 'second', tmp_path,
                              client=transport, assignment_id=assignment)
        result = resumed.resolve_intent(key, 'The same corrected command is complete.')
        assert dispositions[0] == dispositions[1]
        assert resumed.pending() == []
        assert resumed.resolve_intent(key, 'Confirm the existing repair.') == result
        assert len(dispositions) == 2


def test_new_rejected_intents_do_not_reset_unresolved_recovery_age(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(409,
            json={'error': {'code': 'revision_conflict'}}))) as transport:
        oldest = None
        for execution in range(1, 8):
            client = AgentClient('https://horizon.invalid', 'token', str(execution), tmp_path, client=transport)
            with pytest.raises(RuntimeError):
                client.request('POST', '/api/v3/commands', {'operation': 'same-conflict'})
            state = client.recovery_state()
            assert state['pending'] == execution
            assert state['consecutive_observations'] == execution
            oldest = oldest or state['intents'][0]['id']
        with client.connect() as db:
            # This represents a confirmed repair; adding or changing a reject
            # above could never reset the still-unresolved original's budget.
            db.execute("UPDATE intent SET status='resolved' WHERE id=?", (oldest,))
        assert client.recovery_state()['consecutive_observations'] == 1


def test_replayed_request_that_becomes_definitively_invalid_has_no_recovery_blocker(tmp_path):
    available = False
    def response(_):
        return httpx.Response(404 if available else 503,
            json={'error': {'code': 'not_found' if available else 'database_unavailable'}})
    with httpx.Client(transport=httpx.MockTransport(response)) as transport:
        first = AgentClient('https://horizon.invalid', 'token', 'first', tmp_path, client=transport)
        with pytest.raises(RuntimeError):
            first.request('POST', '/api/v3/commands', {'operation': 'work'})
        available = True
        resumed = AgentClient('https://horizon.invalid', 'token', 'next', tmp_path, client=transport)
        assert resumed.replay_pending() == {'completed': [], 'blocked': [], 'pending': 0}
        assert resumed.recovery_state()['pending'] == 0


@pytest.mark.parametrize('target_kind,code', [('self', 'self_delegation'), ('cancelled', 'unavailable_owner')])
def test_rejected_delegation_can_be_corrected_without_journal_recovery(api, tmp_path, target_kind, code):
    server, database, world, run, _, _ = api
    with database.transaction() as conn:
        world.conn = conn
        world.disable_automations(run)
        assignment = world.assignment(run, functions=['planner'])
        target = assignment
        if target_kind == 'cancelled':
            target = world.assignment(run)
            change(conn, 'assignment', target['id'], status='cancelled')
        claim = world.claim()
        root = world.ledger(assignment['id'])[0]

    def response(request):
        result = server.request(request.method, request.url.raw_path.decode(),
            content=request.content, headers=dict(request.headers))
        return httpx.Response(result.status_code, content=result.content)

    with httpx.Client(transport=httpx.MockTransport(response)) as transport:
        client = AgentClient('http://testserver', claim['execution_token'], claim['execution_id'],
            tmp_path / 'journal', client=transport, assignment_id=str(assignment['id']))
        path = f'/api/v3/obligations/{root["id"]}/resolve'
        with pytest.raises(RuntimeError, match=code):
            client.request('POST', path, {'expected_revision': root['revision'], 'status': 'handled',
                'resolution': {'kind': 'delegated', 'assignment_ids': [str(target['id'])],
                               'note': 'Follow up the remaining work.'}})
        assert client.pending() == []
        with database.transaction() as conn:
            unchanged = get(conn, 'obligation', root['id'])
            assert unchanged['status'] == 'open' and unchanged['revision'] == root['revision']

        client.request('POST', path, {'expected_revision': root['revision'], 'status': 'done',
            'resolution': {'kind': 'completed', 'note': 'The bounded planning pass is complete.'}})
        assert client.replay_pending() == {'completed': [], 'blocked': [], 'pending': 0}
        with database.transaction() as conn:
            world.conn = conn
            completed_request(world, claim, 1)
            result = finish(world, claim, client.recovery_state(), reason=None)
            assert result['status'] == 'completed'


def test_rejected_parent_closure_can_be_corrected_without_journal_recovery(api, tmp_path):
    server, database, world, run, _, _ = api
    with database.transaction() as conn:
        world.conn = conn
        world.disable_automations(run)
        assignment = world.assignment(run, role='maintainer')
        parent = get(conn, 'mission', world.mission['id'])
        child = world.service.mission(conn, world.actor, models.MissionCreate(
            project_id=world.project['id'], parent_id=parent['id'], expected_parent_revision=parent['revision'],
            title='One milestone', objective='Review one bounded statement',
            acceptance_criteria=['The statement is reviewed'], delegation_note='Own this statement review.'))
        parent = get(conn, 'mission', parent['id'])
        claim = world.claim()

    def response(request):
        result = server.request(request.method, request.url.raw_path.decode(),
            content=request.content, headers=dict(request.headers))
        return httpx.Response(result.status_code, content=result.content)

    with httpx.Client(transport=httpx.MockTransport(response)) as transport:
        client = AgentClient('http://testserver', claim['execution_token'], claim['execution_id'],
            tmp_path / 'journal', client=transport, assignment_id=str(assignment['id']))

        def close(mission):
            return client.request('POST', '/api/v3/commands', {'operation': 'complete_mission',
                'target_id': str(mission['id']), 'expected_revision': mission['revision'],
                'args': {'note': 'The reviewed evidence satisfies this mission.'}})

        with pytest.raises(RuntimeError, match='open_children'):
            close(parent)
        assert client.pending() == []
        with database.transaction() as conn:
            unchanged = get(conn, 'mission', parent['id'])
            assert unchanged['status'] == 'open' and unchanged['revision'] == parent['revision']

        close(child)
        parent = client.request('GET', f'/api/v3/records/mission/{parent["id"]}')
        close(parent)
        assert client.replay_pending() == {'completed': [], 'blocked': [], 'pending': 0}
        with database.transaction() as conn:
            assert get(conn, 'mission', parent['id'])['status'] == 'completed'


@pytest.mark.parametrize('operation', ['create', 'reopen'])
def test_child_budget_rejection_has_no_mutation_or_journal_blocker(api, tmp_path, operation):
    server, database, world, run, _, _ = api
    with database.transaction() as conn:
        world.conn = conn
        world.disable_automations(run)
        assignment = world.assignment(run, functions=['planner'])
        for number in range(2):
            parent = get(conn, 'mission', world.mission['id'])
            child = world.service.mission(conn, world.actor, models.MissionCreate(
                project_id=world.project['id'], parent_id=parent['id'], expected_parent_revision=parent['revision'],
                title=f'Contract {number}', objective=f'Review bounded contract {number}',
                acceptance_criteria=['Current statement reviewed'], delegation_note='Own this statement review.'))
        child = world.command('complete_mission', child, note='The bounded statement review is complete.')
        parent = get(conn, 'mission', parent['id'])
        parent = world.service.update_mission(conn, world.actor, parent['id'], models.MissionUpdate(
            expected_revision=parent['revision'], max_open_children=1))
        missions = select(tables['mission']).where(tables['mission'].c.parent_id == parent['id']).order_by(
            tables['mission'].c.id)
        before = list(conn.execute(missions).mappings())
        claim = world.claim()

    def response(request):
        result = server.request(request.method, request.url.raw_path.decode(),
            content=request.content, headers=dict(request.headers))
        return httpx.Response(result.status_code, content=result.content)

    with httpx.Client(transport=httpx.MockTransport(response)) as transport:
        client = AgentClient('http://testserver', claim['execution_token'], claim['execution_id'],
            tmp_path / 'journal', client=transport, assignment_id=str(assignment['id']))
        with pytest.raises(RuntimeError, match='child_budget_exhausted'):
            if operation == 'create':
                client.request('POST', '/api/v3/records/mission', {
                    'project_id': str(world.project['id']), 'parent_id': str(parent['id']),
                    'expected_parent_revision': parent['revision'], 'title': 'Unadmitted contract',
                    'objective': 'Review another contract', 'acceptance_criteria': ['Statement reviewed'],
                    'delegation_note': 'Own this distinct statement review.'})
            else:
                client.request('POST', '/api/v3/commands', {'operation': 'reopen_mission',
                    'target_id': str(child['id']), 'expected_revision': child['revision'],
                    'args': {'note': 'A new review finding needs repair.'}})
        assert client.pending() == []
        assert client.replay_pending() == {'completed': [], 'blocked': [], 'pending': 0}
        with client.connect() as local:
            rejected = local.execute('SELECT status,response FROM intent').fetchone()
            assert rejected['status'] == 'invalid'
            assert json.loads(rejected['response'])['error']['code'] == 'child_budget_exhausted'
        with database.transaction() as conn:
            assert list(conn.execute(missions).mappings()) == before
            assert get(conn, 'mission', parent['id']) == parent


def test_control_yield_reconciles_legacy_journal_before_terminal_receipt(journal, repository, tmp_path):
    from archon_horizon.pipeline.worker.contracts import ExecutionGrant
    from archon_horizon.pipeline.worker.daemon import HarnessConfig, WorkerDaemon
    from archon_horizon.pipeline.worker.transport import WorkerTransport

    assignment = str(uuid4())
    state = tmp_path / 'provider-home' / 'assignments' / assignment / 'api-intents'
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404,
            json={'error': {'code': 'not_found'}}))) as transport:
        old = AgentClient('http://testserver', 'scoped', 'old', state, client=transport, assignment_id=assignment)
        with pytest.raises(RuntimeError):
            old.request('POST', '/api/v3/commands', {'operation': 'work'})
        with old.connect() as db:
            db.execute("UPDATE intent SET status='rejected'")
    grant = ExecutionGrant('execution-1', assignment, 1, 3, 'harness-1', 'workspace-1',
        str(repository), 'repo-1', 'Finish journal recovery', execution_token='scoped')
    observations = []
    def response(request):
        if request.url.path.endswith('/reviewer-accounts'):
            return httpx.Response(200, json={'execution_id': grant.execution_id, 'accounts': []})
        if request.url.path.endswith('/claim'):
            return httpx.Response(200, json={'execution': grant.__dict__})
        if request.url.path.endswith('/heartbeat'):
            return httpx.Response(200, json={'lease_seconds': 30, 'stop': False, 'yield': True, 'continue': False})
        observations.append(json.loads(request.content))
        return httpx.Response(200, json={'acknowledged': True})
    adapter = SimpleNamespace(provider='codex_exec', sandbox_mode='workspace_write', command=lambda **_: [
        sys.executable, '-c', 'import json,time; print(json.dumps({"type":"thread.started",'
        '"thread_id":"retained-context"}),flush=True); time.sleep(30)'])
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        daemon = WorkerDaemon(host_id='host-1', journal=journal,
            transport=WorkerTransport('http://testserver', 'host-secret', client=client),
            harnesses={'harness-1': HarnessConfig(adapter, tmp_path / 'provider-home',
                                               tmp_path / 'scratch', unrestricted=True)},
            workspace_roots=(repository,))
        outcome = daemon.run_once()
        assert outcome == 'yielded', [row.get('payload') for row in observations if row.get('kind') == 'execution_finished']
    receipt = next(row['payload'] for row in reversed(observations) if row['kind'] == 'execution_finished')
    assert receipt['reason'] == 'control_plane_yield'
    assert receipt['intent_reconciliation']['pending'] == 0
    with old.connect() as db:
        assert db.execute('SELECT status FROM intent').fetchone()[0] == 'invalid'


def finish(world, claim, state, reason='agent_intents_require_reconciliation'):
    operation = WorkerOperation(operation_id=uuid4(), execution_id=claim['execution_id'], epoch=claim['epoch'],
        kind='execution_finished', occurred_at=datetime.now(timezone.utc).timestamp(),
        payload={'status': 'yielded', 'reason': reason, 'provider_thread_id': 'retained-context',
                 'intent_reconciliation': state})
    return handle(world.conn, world.host_actor, operation, world.service, world.scheduler)


def completed_request(world, claim, number):
    return create(world.conn, 'provider_request', provider_thread_id=UUID(claim['provider_thread_record_id']),
        execution_id=UUID(claim['execution_id']), number=number, reason='continuation', status='completed')


def settle_deliverable(world, assignment):
    root = world.ledger(assignment['id'])[0]
    change(world.conn, 'obligation', root['id'], status='done', resolution={
        'kind': 'completed', 'note': 'Delivered the substantive work.', 'evidence': []})


@pytest.mark.parametrize('role,functions', [('worker', []), ('worker', ['planner']), ('maintainer', [])])
def test_same_unresolved_journal_reuses_blocker_and_fails_boundedly(world, role, functions):
    run = world.run(retry_policy={'max_no_progress_requests': 3})
    world.disable_automations(run)
    assignment = world.assignment(run, role=role, functions=functions)
    settle_deliverable(world, assignment)
    identifier = blocker_id(assignment['id'])
    intent = str(uuid4())
    for attempt in range(1, 4):
        if attempt > 1:
            world.command('update_assignment', get(world.conn, 'assignment', assignment['id']), not_before=None)
        claim = world.claim()
        assert claim['assignment_id'] == str(assignment['id'])
        completed_request(world, claim, attempt)
        if attempt > 1:
            # Reproduce the misleading server-only "repair" from the live loop.
            change(world.conn, 'obligation', identifier, status='done', resolution={
                'kind': 'completed', 'note': 'Historical reject was already accounted for.', 'evidence': []})
        result = finish(world, claim, {'pending': 1, 'fingerprint': 'a' * 64,
            'consecutive_observations': attempt, 'intents': [{'id': intent, 'status': 'rejected'}]})
        blocker = get(world.conn, 'obligation', identifier)
        assert blocker['status'] == 'open'
        assert f'agent resolve-intent {intent} --note' in blocker['description']
        assert sum(row['description'].startswith(JOURNAL_BLOCKER_PREFIX)
                   for row in world.ledger(assignment['id'])) == 1
    assert result['status'] == 'failed'
    assert get(world.conn, 'execution', claim['execution_id'])['failure']['code'] == 'agent_intent_reconciliation_stalled'
    assert world.claim() is None


def test_host_confirmed_journal_repair_finishes_and_old_receipt_cannot_reopen(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    settle_deliverable(world, assignment)
    first = world.claim()
    completed_request(world, first, 1)
    unresolved = {'pending': 1, 'fingerprint': 'b' * 64, 'consecutive_observations': 1,
                  'intents': [{'id': str(uuid4()), 'status': 'pending'}]}
    finish(world, first, unresolved)
    identifier = blocker_id(assignment['id'])
    world.command('update_assignment', get(world.conn, 'assignment', assignment['id']), not_before=None)
    second = world.claim()
    completed_request(world, second, 2)
    result = finish(world, second, {'pending': 0, 'fingerprint': None,
        'consecutive_observations': 0, 'intents': []}, reason=None)
    assert result['status'] == 'completed'
    assert get(world.conn, 'obligation', identifier)['status'] == 'done'
    finish(world, first, unresolved)
    assert get(world.conn, 'obligation', identifier)['status'] == 'done'
    assert get(world.conn, 'assignment', assignment['id'])['status'] == 'completed'


def test_empty_journal_snapshot_overrides_stale_reconciliation_reason(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    settle_deliverable(world, assignment)

    first = world.claim()
    completed_request(world, first, 1)
    unresolved = {'pending': 1, 'fingerprint': 'c' * 64, 'consecutive_observations': 1,
                  'intents': [{'id': str(uuid4()), 'status': 'pending'}]}
    finish(world, first, unresolved)
    identifier = blocker_id(assignment['id'])
    assert get(world.conn, 'obligation', identifier)['status'] == 'open'

    world.command('update_assignment', get(world.conn, 'assignment', assignment['id']), not_before=None)
    second = world.claim()
    completed_request(world, second, 2)
    settled = {'pending': 0, 'fingerprint': None, 'consecutive_observations': 0, 'intents': []}
    # The daemon can preserve the earlier reason while attaching the fresh
    # empty snapshot. The snapshot must determine journal_pending.
    result = finish(world, second, settled)

    assert result['status'] == 'completed'
    assert get(world.conn, 'obligation', identifier)['status'] == 'done'
    assert get(world.conn, 'assignment', assignment['id'])['status'] == 'completed'


def test_maintainer_cannot_delegate_away_its_unresolved_local_journal(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, role='maintainer')
    planner = world.assignment(run, functions=['planner'])
    settle_deliverable(world, assignment)
    claim = world.claim()
    assert claim['assignment_id'] == str(assignment['id'])
    completed_request(world, claim, 1)
    result = finish(world, claim, {'pending': 1, 'fingerprint': 'c' * 64,
        'consecutive_observations': 1, 'intents': [{'id': str(uuid4()), 'status': 'rejected'}]})
    assert result['status'] == 'pending'
    assert get(world.conn, 'obligation', blocker_id(assignment['id']))['status'] == 'open'
    assert len(world.ledger(planner['id'])) == 1


def test_six_hundred_admin_resolutions_are_not_substantive_progress(world):
    run = world.run(retry_policy={'max_no_progress_requests': 3})
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    for number in range(1, 4):
        completed_request(world, claim, number)
    table = tables['obligation']
    world.conn.execute(insert(table), [{'id': uuid4(), 'assignment_id': assignment['id'], 'number': n + 2,
        'kind': 'blocker' if n % 2 else 'decision', 'description': (
            JOURNAL_BLOCKER_PREFIX if n % 2 else INTENT_REPAIR_PREFIX) + str(n), 'status': 'done',
        'resolution': {'kind': 'completed', 'note': 'Repeated administrative resolution.', 'evidence': []}}
        for n in range(610)])
    assert world.service.progress_stalled(world.conn, assignment['id'], 3)
    settle_deliverable(world, assignment)
    assert not world.service.progress_stalled(world.conn, assignment['id'], 3)
