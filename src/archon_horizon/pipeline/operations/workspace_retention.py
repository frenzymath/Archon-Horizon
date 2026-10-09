"""Retire only settled managed workspaces; suspended sessions are never disposable."""
from pathlib import PurePosixPath

from sqlalchemy import select, update, func

from ..auth import require_project
from ..errors import DomainError
from ..persistence.records import change, emit, get
from ..persistence.schema import tables


def retire(conn, actor, workspace, service, note):
    require_project(conn, actor, workspace['project_id'], 'maintainer')
    host = get(conn, 'host', workspace['host_id'])
    path = PurePosixPath(workspace['path'])
    if path.parent != PurePosixPath(host['workspace_root']) / 'assignments':
        raise DomainError('workspace_not_managed', 'Only generated assignment workspaces can be retired for deletion', 422)
    if workspace['status'] != 'ready':
        raise DomainError('workspace_not_ready', 'Retire a settled ready workspace', 409)
    thread, assignment, execution, obligation, publication, run = (tables[n] for n in
        ('provider_thread', 'assignment', 'execution', 'obligation', 'publication', 'run'))
    owners = list(conn.execute(select(assignment.c.id).join(thread,
        thread.c.assignment_id == assignment.c.id).where(thread.c.workspace_id == workspace['id']).distinct()).scalars())
    active = conn.execute(select(assignment.c.id).where(assignment.c.id.in_(owners),
        assignment.c.status.in_(('pending', 'running', 'stopping'))).limit(1)).first()
    uncertain = conn.execute(select(execution.c.id).where(execution.c.workspace_id == workspace['id'],
        execution.c.stop_confirmed_at.is_(None)).limit(1)).first()
    open_ledger = conn.execute(select(obligation.c.id).where(obligation.c.assignment_id.in_(owners),
        obligation.c.status == 'open').limit(1)).first()
    pinned = conn.execute(select(run.c.id).where(run.c.status.in_(('active','paused','draining','stopping')),
        run.c.phase['source_workspace_id'].astext == str(workspace['id'])).limit(1)).first()
    unpublished = conn.execute(select(publication.c.id).where(publication.c.requested_by_assignment_id.in_(owners),
        publication.c.status.in_(('pending','running','failed'))).limit(1)).first()
    if active or uncertain or open_ledger or pinned or unpublished or any(service.pending_deliveries(conn, owner) for owner in owners):
        raise DomainError('workspace_retained', 'Live or suspended owners, unresolved work, pinned inputs and unpublished sources must be preserved', 409)
    conn.execute(update(thread).where(thread.c.workspace_id == workspace['id'],
        thread.c.status.in_(('creating','available'))).values(status='unavailable', recovery_note=note))
    row = change(conn, 'workspace', workspace['id'], workspace['revision'], status='retired', cleanup_error=None)
    emit(conn, actor.id, workspace['project_id'], 'workspace', row, ['status'], note=note)
    return row
