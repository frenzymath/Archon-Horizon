from uuid import uuid4

from archon_horizon.pipeline.persistence.records import change, create
from test_pipeline_api import api, api_database, auth, mutate
from test_pipeline_milestones import report


def test_verification_request_host_result_and_idempotent_recovery(api):
    client, database, world, _, user_token, host_token = api
    with database.transaction() as conn:
        change(conn, 'project', world.project['id'], workflow='milestones')
        change(conn, 'host', world.host['id'], health={'capabilities': {'milestone_verification': 1}})
        workspace = create(conn, 'workspace', project_id=world.project['id'], host_id=world.host['id'],
            repository_id=world.document['source_repository_id'], path='/roadmap', branch_name='main',
            base_commit_oid='a' * 40, status='ready')
    data = {'workspace_id': str(workspace['id']), 'source_commit_oid': 'a' * 40, 'base_commit_oid': 'b' * 40}
    queued = mutate(client, '/api/v3/milestones/verifications', data, user_token)
    assert queued.status_code == 200, queued.text
    identifier = queued.json()['id']
    assert mutate(client, '/api/v3/milestones/verifications', data, user_token).json()['id'] == identifier
    claim_path = '/api/v3/worker/milestone-jobs/claim'
    assert mutate(client, claim_path, {'host_id': str(world.host['id'])}, user_token).status_code == 403
    claimed = mutate(client, claim_path, {'host_id': str(world.host['id'])}, host_token)
    assert claimed.status_code == 200, claimed.text
    job = claimed.json()['job']
    detail = client.get('/api/v3/milestones/verifications/' + identifier, headers=auth(user_token))
    assert detail.status_code == 200 and 'claim_token' not in detail.json()
    body = {'claim_token': job['claim_token'], 'report': report().model_dump(mode='json')}
    path = '/api/v3/worker/milestone-jobs/' + identifier + '/finish'
    assert mutate(client, path, body, user_token).status_code == 403
    key = str(uuid4())
    finished = mutate(client, path, body, host_token, key)
    assert finished.status_code == 200, finished.text
    assert finished.json()['status'] == 'completed'
    replay = mutate(client, path, body, host_token, key)
    assert replay.status_code == 200 and replay.json() == finished.json()
    proof = client.get('/api/v3/milestones/checks/' + finished.json()['check_id'], headers=auth(user_token))
    assert proof.status_code == 200 and proof.json()['report']['manifest_digest'] == report().manifest_digest
    assert client.get('/api/v3/milestones/checks/' + finished.json()['check_id']).status_code == 401
