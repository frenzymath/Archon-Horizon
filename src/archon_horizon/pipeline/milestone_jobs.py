"""Leased verification requests; only the owning host can publish their results."""

from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import select, or_, func

from .auth import require_host, require_project
from .errors import DomainError
from .milestones import OID, CheckReport, CheckSubmission, enabled, register_check
from .models import Contract, RelativePath
from .records import change, create, get, same_project, emit
from .schema import tables


class VerificationRequest(Contract):
    workspace_id: UUID
    source_commit_oid: OID
    base_commit_oid: OID
    solution_workspace_id: UUID | None = None
    implementation_commit_oid: OID | None = None
    comparator_config: RelativePath | None = None

    @model_validator(mode='after')
    def proof_inputs(self):
        if len({bool(self.solution_workspace_id), bool(self.implementation_commit_oid), bool(self.comparator_config)}) != 1:
            raise ValueError('Proof checks require solution workspace, exact implementation commit and comparator config')
        return self


class Claim(Contract):
    host_id: UUID


class Lease(Contract):
    claim_token: UUID


class Finish(Lease):
    report: CheckReport | None = None
    error: str | None = Field(default=None, min_length=1, max_length=6000)

    @model_validator(mode='after')
    def outcome(self):
        if (self.report is None) == (self.error is None):
            raise ValueError('Supply exactly one successful report or failure diagnostic')
        return self


def queue(conn, actor, data):
    workspace = get(conn, 'workspace', data.workspace_id)
    require_project(conn, actor, workspace['project_id'], 'worker')
    repo = get(conn, 'repository', workspace['repository_id'])
    host = get(conn, 'host', workspace['host_id'])
    if (not enabled(conn, workspace['project_id']) or repo['purpose'] != 'knowledge' or workspace['status'] != 'ready'):
        raise DomainError('invalid_milestone_workspace', 'Select a ready roadmap workspace in a milestone project', 422)
    if (host.get('health') or {}).get('capabilities', {}).get('milestone_verification') != 1:
        raise DomainError('milestone_checker_unavailable', 'Enable milestone_checks on this build host first', 409)
    if data.solution_workspace_id:
        solution = same_project(conn, 'workspace', data.solution_workspace_id, workspace['project_id'])
        if solution['host_id'] != workspace['host_id'] or solution['status'] != 'ready':
            raise DomainError('invalid_milestone_workspace', 'The solution needs a ready workspace on the same build host', 422)
    table = tables['milestone_job']
    request = data.model_dump(mode='json')
    existing = conn.execute(select(table).where(table.c.workspace_id == workspace['id'], table.c.request == request,
        table.c.status.in_(['queued', 'running', 'completed'])).order_by(table.c.created_at.desc()).limit(1)).mappings().first()
    if existing:
        return dict(existing)
    row = create(conn, 'milestone_job', project_id=workspace['project_id'], host_id=workspace['host_id'],
        workspace_id=workspace['id'], principal_id=actor.id, request=request)
    emit(conn, actor.id, row['project_id'], None, row, ['milestone_job'])
    return row


def claim(conn, actor, data):
    require_host(conn, actor, data.host_id)
    if get(conn, 'host', data.host_id)['mode'] != 'enabled':
        return None
    table = tables['milestone_job']
    now = conn.execute(select(func.now())).scalar_one()
    jobs = conn.execute(select(table).where(table.c.host_id == data.host_id,
        or_(table.c.status == 'queued', (table.c.status == 'running') & (table.c.lease_until < now)))
        .order_by(table.c.created_at).with_for_update(skip_locked=True).limit(10)).mappings()
    for row in jobs:
        if get(conn, 'workspace', row['workspace_id'])['status'] != 'ready':
            change(conn, 'milestone_job', row['id'], status='failed', error='Roadmap workspace is no longer ready')
            continue
        if row['attempts'] >= 3:
            change(conn, 'milestone_job', row['id'], status='failed', error='Build host lost three verification leases')
            continue
        job = change(conn, 'milestone_job', row['id'], status='running', attempts=row['attempts'] + 1,
            claim_token=uuid4(), lease_until=now + timedelta(seconds=300))
        job['workspace'] = get(conn, 'workspace', job['workspace_id'])
        job['solution_workspace'] = (get(conn, 'workspace', job['request']['solution_workspace_id'])
                                     if job['request']['solution_workspace_id'] else None)
        return job
    return None


def live_job(conn, actor, identifier, token):
    job = get(conn, 'milestone_job', identifier, lock=True)
    require_host(conn, actor, job['host_id'])
    now = conn.execute(select(func.now())).scalar_one()
    if job['status'] != 'running' or job['claim_token'] != token or job['lease_until'] <= now:
        raise DomainError('milestone_lease_lost', 'The verification lease is no longer current', 409)
    return job, now


def heartbeat(conn, actor, identifier, data):
    job, now = live_job(conn, actor, identifier, data.claim_token)
    return change(conn, 'milestone_job', job['id'], lease_until=now + timedelta(seconds=300))


def finish(conn, actor, identifier, data):
    job, _ = live_job(conn, actor, identifier, data.claim_token)
    checked = None
    if data.report:
        request = job['request']
        if (data.report.source_commit_oid != request['source_commit_oid']
                or data.report.base_commit_oid != request['base_commit_oid']
                or data.report.implementation_commit_oid != request['implementation_commit_oid']):
            raise DomainError('milestone_check_mismatch', 'Verification must cover the requested immutable inputs', 422)
        checked = register_check(conn, actor, CheckSubmission(workspace_id=job['workspace_id'], report=data.report))
    row = change(conn, 'milestone_job', identifier, status='completed' if checked else 'failed',
        check_id=checked['id'] if checked else None, error=data.error, lease_until=None)
    emit(conn, actor.id, row['project_id'], None, row, ['milestone_job'])
    return row
