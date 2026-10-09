"""Source-bound compiler receipts, independent of agent planning policy.

The historical milestone_check table also stores library receipts. Retaining
that storage identity preserves immutable evidence and existing references;
the public Lean verification API and graph workflow do not require milestones.
Legacy receipt fields remain readable for explicitly retained older projects.
"""

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool
from sqlalchemy import select

from ..auth import require_host
from ..errors import DomainError
from ..models import Contract, Text
from ..persistence.records import create, emit, get
from ..persistence.schema import tables

LeanName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_']*(\.[A-Za-z_][A-Za-z0-9_']*)*$", max_length=512)]


def digest(value):
    """Preserve canonical receipt hashes used by existing immutable evidence."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


OID = Annotated[str, Field(pattern=r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')]



SHA = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]



FOUNDATIONS = {'propext', 'Classical.choice', 'Quot.sound'}



class ProofClaim(Contract):
    status: Literal['conditional', 'complete']
    check_id: UUID | None = None
    declarations: list[LeanName] = Field(min_length=1, max_length=100)



class CheckReport(Contract):
    schema_version: Literal[1] = 1
    source_commit_oid: OID
    base_commit_oid: OID
    kind: Literal['route', 'contract', 'graph', 'proof', 'library']
    manifest_digest: SHA
    base_manifest_digest: SHA
    ancestor_commits: list[OID] = Field(max_length=10000)
    # Empty for general library checks; retained for old immutable receipts.
    milestone_keys: list[str] = Field(default_factory=list, max_length=1000)
    objective_paths: list[str] = Field(default_factory=list, max_length=100)
    compiled: StrictBool
    targets: dict[str, list[str]] = Field(default_factory=dict)
    definitions: dict[str, list[str]] = Field(default_factory=dict)
    types: dict[str, list[str]] = Field(default_factory=dict)
    declaration_modules: dict[str, str] = Field(default_factory=dict)
    direct_admissions: list[str] = Field(default_factory=list)
    proof_claims: dict[str, ProofClaim] = Field(default_factory=dict)
    implementation_commit_oid: OID | None = None
    comparator_passed: StrictBool = False
    toolchain: Text



class CheckSubmission(Contract):
    workspace_id: UUID
    report: CheckReport



def register_check(conn, actor, data: CheckSubmission):
    workspace = get(conn, 'workspace', data.workspace_id)
    require_host(conn, actor, workspace['host_id'])
    if workspace['status'] != 'ready':
        raise DomainError('workspace_unavailable', 'Verification requires a registered ready workspace', 409)
    repo = get(conn, 'repository', workspace['repository_id'])
    if repo['purpose'] != ('library' if data.report.kind == 'library' else 'knowledge'):
        raise DomainError('invalid_verification_repository', 'Receipt repository must match its verification kind', 422)
    report = data.report.model_dump(mode='json')
    if not report['compiled']:
        raise DomainError('milestone_check_failed', 'Only successful checks can attest a revision', 422)
    if report['kind'] == 'library' and (report['direct_admissions'] or any(set(a) - FOUNDATIONS for a in report['targets'].values())):
        raise DomainError('library_admissions', 'Library receipts require complete axiom closure without admissions', 422)
    if report['kind'] == 'graph' and report['manifest_digest'] != report['base_manifest_digest']:
        raise DomainError('contract_change_misclassified', 'A changed contract cannot use graph-only review', 422)
    for axioms in [*report['definitions'].values(), *report['types'].values()]:
        if set(axioms) - FOUNDATIONS:
            raise DomainError('admitted_definition', 'Definitions and statement types must have foundation-only axiom closure', 422)
    if any(set(axioms) - FOUNDATIONS - {'sorryAx'} for axioms in report['targets'].values()):
        raise DomainError('untrusted_milestone', 'Milestone targets contain unapproved axioms', 422)
    if report['kind'] == 'proof' and (not report['comparator_passed'] or not report['implementation_commit_oid']):
        raise DomainError('comparator_required', 'Proof receipts require a compared implementation revision', 422)
    if report['kind'] != 'proof':
        for name, claim in report['proof_claims'].items():
            evidence = get(conn, 'milestone_check', claim['check_id']) if claim['check_id'] else None
            if (not evidence or evidence['repository_id'] != repo['id'] or evidence['kind'] != 'proof'
                    or evidence['report']['manifest_digest'] != report['manifest_digest']):
                raise DomainError('proof_evidence_missing', f'{name} needs matching Comparator and axiom evidence', 409)
            proof = evidence['report']
            declarations = claim['declarations']
            if (not set(declarations) <= set(proof['targets'])
                    or set(declarations) & set(proof.get('direct_admissions', []))):
                raise DomainError('proof_evidence_incomplete', f'{name} still has direct or unchecked admissions', 409)
            actual = 'conditional' if any('sorryAx' in proof['targets'][d] for d in declarations) else 'complete'
            if claim['status'] != actual:
                raise DomainError('proof_status_mismatch', f'{name} is {actual}, not {claim["status"]}', 409)
    checksum = digest(report)
    table = tables['milestone_check']
    old = conn.execute(select(table).where(table.c.repository_id == repo['id'], table.c.report_sha256 == checksum)).mappings().first()
    if old:
        return dict(old)
    row = create(conn, 'milestone_check', project_id=repo['project_id'], repository_id=repo['id'],
        principal_id=actor.id, source_commit_oid=report['source_commit_oid'], base_commit_oid=report['base_commit_oid'],
        kind=report['kind'], report=report, report_sha256=checksum)
    emit(conn, actor.id, repo['project_id'], None, row, ['milestone_check'])
    return row



def head_check(conn, item):
    table = tables['milestone_check']
    return conn.execute(select(table).where(table.c.repository_id == item['repository_id'],
        table.c.source_commit_oid == item['head_commit_oid'], table.c.kind != 'proof')
        .order_by(table.c.created_at.desc()).limit(1)).mappings().first()
