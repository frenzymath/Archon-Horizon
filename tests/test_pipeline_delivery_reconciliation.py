from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from archon_horizon.pipeline.auth import Actor
from archon_horizon.pipeline.commands import Command, execute
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.persistence.records import create, get
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import service_database, world  # noqa: F401


def operation(world, status='uncertain', kind='forge_create'):
    return create(world.conn, 'outbox_operation', actor_principal_id=world.actor.id,
        project_id=world.project['id'], kind=kind, status=status, schema_version=1,
        idempotency_key='original-key', payload={'repository_id': str(world.workspace_repo['id']),
        'kind': 'pull_request', 'head': 'a' * 40, 'base': 'main'}, retry_count=61,
        retry_at=datetime.now(timezone.utc) + timedelta(minutes=5))


def test_admin_can_record_remote_absence_without_changing_or_resending_original_intent(world):
    original = operation(world)
    note = 'Inspected Forge request log request-42 and all PR pages at 06:45Z; the only POST returned 422 with no PR created.'
    result = world.command('reconcile_delivery_absent', original, note=note)
    assert result['status'] == 'failed' and result['retry_at'] is None
    assert result['failure'] == {'kind': 'transport', 'code': 'remote_absence_confirmed', 'message': note}
    for field in ('payload', 'actor_principal_id', 'idempotency_key', 'retry_count', 'lease_epoch', 'result_ref_id'):
        assert result[field] == original[field]
    assert result['revision'] == original['revision'] + 1
    assert len(world.conn.execute(select(tables['outbox_operation'])).all()) == 1
    events = world.conn.execute(select(tables['event']).where(tables['event'].c.actor_principal_id == world.actor.id)).mappings()
    assert any(note in event['payload'].get('note', '') and str(original['id']) in event['payload']['note']
               for event in events)


def test_project_maintainer_cannot_assert_delivery_absence(world):
    original = operation(world)
    principal = create(world.conn, 'principal', kind='human', display_name='Maintainer', username='maintainer')
    world.conn.execute(tables['project_grant'].insert().values(principal_id=principal['id'],
        project_id=world.project['id'], role='maintainer'))
    actor = Actor(principal['id'], 'human', {'username': 'maintainer'}, 'api_key')
    with pytest.raises(DomainError) as error:
        execute(world.conn, actor, Command(operation='reconcile_delivery_absent', target_id=original['id'],
            expected_revision=original['revision'], args={'note': 'I inspected remote records'}), world.service, world.scheduler)
    assert error.value.code == 'forbidden'
    assert get(world.conn, 'outbox_operation', original['id']) == original


@pytest.mark.parametrize('status', ['pending', 'running', 'failed', 'completed', 'cancelled'])
def test_absence_reconciliation_rejects_non_uncertain_states(world, status):
    original = operation(world, status=status)
    with pytest.raises(DomainError) as error:
        world.command('reconcile_delivery_absent', original, note='Inspected remote evidence')
    assert error.value.code == 'delivery_not_uncertain'
    assert get(world.conn, 'outbox_operation', original['id']) == original


def test_absence_reconciliation_requires_evidence_note_and_correct_revision(world):
    original = operation(world)
    with pytest.raises(DomainError) as error:
        world.command('reconcile_delivery_absent', original, note='   ')
    assert error.value.code == 'missing_recovery_note'
    with pytest.raises(DomainError) as error:
        world.command('reconcile_delivery_absent', {**original, 'revision': original['revision'] + 1},
                      note='Inspected remote evidence')
    assert error.value.code == 'revision_conflict'
    assert get(world.conn, 'outbox_operation', original['id']) == original


def test_absence_reconciliation_excludes_other_lifecycles(world):
    original = operation(world, kind='goal_update')
    with pytest.raises(DomainError) as error:
        world.command('reconcile_delivery_absent', original, note='Inspected remote evidence')
    assert error.value.code == 'invalid_delivery'
