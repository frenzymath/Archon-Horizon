"""Verified milestone receipts, review coverage and baseline acceptance."""

from datetime import datetime, timezone
import json
from typing import Annotated, Literal
from uuid import UUID
from urllib.parse import quote

from pydantic import Field, StrictBool
from sqlalchemy import select

from ..auth import require_host, require_project
from ..errors import DomainError
from .milestone_sources import digest, source_manifest
from .lean_checks import (OID, SHA, FOUNDATIONS, ProofClaim, CheckReport, CheckSubmission, register_check, head_check)
from ..models import Contract, Text
from ..persistence.records import create, emit, get, json_value, same_project
from ..persistence.schema import tables

CONTRACT_REVIEWERS = {'statement-fidelity', 'definitions', 'decomposition', 'library-api'}


class AcceptBaseline(Contract):
    document_id: UUID
    expected_revision: int = Field(ge=1)
    check_id: UUID
    route_gate_id: UUID | None = None
    contract_gate_id: UUID
    previous_snapshot_id: UUID | None = None
    note: Text


def enabled(conn, project_id):
    return get(conn, 'project', project_id)['workflow'] == 'milestones'


def projected_manifest(conn, repository_id, commit):
    table = tables['source_projection']
    rows = conn.execute(select(table).where(table.c.repository_id == repository_id)).mappings().all()
    if not rows or any(row['source_commit_oid'] != commit for row in rows):
        raise DomainError('milestone_index_stale', 'Index the exact accepted roadmap revision before approving it', 409)
    sources = {row['source_path']: ('---\n' + json.dumps(row['metadata']) + '\n---\n\n' + row['markdown']
        if row['source_path'].startswith(('nodes/', 'objectives/')) else row['markdown']) for row in rows}
    return source_manifest(sources)


def review_panel(conn, item, policy, *, review_payloads=None):
    repo = get(conn, 'repository', item['repository_id'])
    if repo['purpose'] != 'knowledge' or not enabled(conn, repo['project_id']):
        return None
    checked = head_check(conn, item)
    if not checked:
        return {'required': ['milestone-check'], 'reviewed': [], 'missing': ['milestone-check']}
    if policy.get('specialist_mode') == 'advisory':
        return {'required': [], 'reviewed': [], 'missing': [], 'check_id': str(checked['id']),
                'base_commit_oid': checked['base_commit_oid']}
    if checked['kind'] == 'graph':
        return {'required': [], 'reviewed': [], 'missing': [], 'check_id': str(checked['id']),
                'base_commit_oid': checked['base_commit_oid']}
    perspectives = {'decomposition'} if checked['kind'] == 'route' else CONTRACT_REVIEWERS
    route_missing = []
    if checked['kind'] == 'contract':
        gate, other = tables['review_gate'], tables['forge_item']
        routes = conn.execute(select(gate.c.id).join(other, other.c.id == gate.c.forge_item_id).where(
            other.c.repository_id == repo['id'], other.c.status == 'merged', gate.c.status == 'accepted',
            other.c.head_commit_oid.in_(checked['report']['ancestor_commits']))).scalars()
        for identifier in routes:
            try:
                route = accepted_gate(conn, identifier, repo['id'], checked['report'], 'route')
            except DomainError:
                continue
            if set(checked['report']['milestone_keys']) <= set(route['report']['milestone_keys']):
                break
        else:
            route_missing = ['accepted-route']
    descriptor, link, review = (tables[n] for n in ('reviewer_descriptor', 'review_policy_reviewer', 'forge_review'))
    required = list(conn.execute(select(descriptor).join(link).where(link.c.review_policy_id == policy['id'],
        descriptor.c.enabled.is_(True))).mappings())
    # Preset prefixes are allowed; arbitrary descriptors cannot impersonate required perspectives.
    configured = {name: [d for d in required if d['slug'] == name or d['slug'].endswith('-' + name)] for name in perspectives}
    covered = set()
    invalid = []
    from ..review.decisions import _carry_source, _current_approval, _review_history
    if review_payloads is None:
        _, review_payloads = _review_history(conn, item)
    for name, descriptors in configured.items():
        for d in descriptors:
            latest = conn.execute(select(review).where(review.c.forge_item_id == item['id'],
                review.c.commit_oid == item['head_commit_oid'], review.c.reviewer_descriptor_id == d['id'])
                .order_by(review.c.observed_at.desc(), review.c.created_at.desc()).limit(1)).mappings().first()
            if latest:
                try:
                    _carry_source(conn, item, policy, latest['id'])
                    if not _current_approval(conn, latest, review_payloads.get(latest['id']), item, policy):
                        raise DomainError('invalid_specialist_provenance',
                            'The delivered specialist review must pin the current head, target and policy', 409)
                except DomainError as error:
                    if latest['verdict'] == 'approved' or review_payloads.get(latest['id'], {}).get('rubric_verdict') == 'approved':
                        invalid.append({'review_id': str(latest['id']), 'reviewer_descriptor_id': str(d['id']),
                            'descriptor': d['slug'], 'dimension': name, 'code': error.code, 'reason': error.message})
                    continue
                covered.add(name)
    return {'required': sorted(perspectives) + route_missing, 'reviewed': sorted(covered),
            'missing': sorted(perspectives - covered) + route_missing,
            'check_id': str(checked['id']), 'base_commit_oid': checked['base_commit_oid'], 'invalid_reviews': invalid}


def accepted_gate(conn, identifier, repository_id, report, kind):
    gate = get(conn, 'review_gate', identifier)
    item = get(conn, 'forge_item', gate['forge_item_id'])
    if (item['repository_id'] != repository_id or item['status'] != 'merged' or gate['status'] != 'accepted'
            or gate['accepted_commit_oid'] != item['head_commit_oid']
            or item['head_commit_oid'] not in report['ancestor_commits']):
        raise DomainError('milestone_review_missing', 'A merged, accepted ancestor PR is required', 409)
    check = head_check(conn, item)
    if not check or check['kind'] != kind:
        raise DomainError('milestone_review_kind', f'A reviewed {kind} PR is required', 409)
    policy = get(conn, 'review_policy', gate['policy_id'])
    revision = get(conn, 'record_revision', gate['policy_revision_id'])
    panel = review_panel(conn, item, policy)
    if not policy['enabled'] or revision['object_revision'] != policy['revision'] or panel is None or panel['missing']:
        raise DomainError('milestone_review_stale', 'Required current-rubric specialist approvals are missing', 409)
    return check


def accept_baseline(conn, actor, service, data: AcceptBaseline):
    document = get(conn, 'document', data.document_id, lock=True)
    project_id = document['project_id']
    require_project(conn, actor, project_id, 'maintainer')
    if not enabled(conn, project_id) or document['kind'] != 'roadmap':
        raise DomainError('invalid_milestone_objective', 'Select a milestone workflow roadmap objective', 422)
    if document['revision'] != data.expected_revision:
        raise DomainError('revision_conflict', 'The objective changed; review the current version', 409)
    run_table = tables['run']
    automatic = select(run_table.c.id).where(run_table.c.objective_id == document['id'],
        run_table.c.orchestration == 'objective', run_table.c.auto_advance.is_(True), run_table.c.status == 'active')
    if actor.kind == 'agent':
        from ..auth import live_execution
        automatic = automatic.where(run_table.c.id == live_execution(conn, actor)['run_id'])
    automatic = conn.execute(automatic).first() is not None
    if not data.previous_snapshot_id and actor.kind != 'human' and not automatic:
        raise DomainError('human_approval_required', 'The initial milestone baseline requires human approval', 403)
    receipt = same_project(conn, 'milestone_check', data.check_id, project_id)
    report = receipt['report']
    if receipt['repository_id'] != document['source_repository_id'] or receipt['source_commit_oid'] != document['source_commit_oid']:
        raise DomainError('milestone_check_stale', 'Verification must cover the indexed objective revision', 409)
    manifest = projected_manifest(conn, receipt['repository_id'], receipt['source_commit_oid'])
    if report['manifest_digest'] != manifest['digest'] or receipt['kind'] not in {'contract', 'graph'}:
        raise DomainError('milestone_check_stale', 'Verification must cover this complete contract manifest', 409)
    path = document['source_path']
    objective = manifest['objectives'].get(path)
    active = {k: n for k, n in manifest['nodes'].items() if n['milestone'] and
              n['milestone']['objective'] == path and not n['milestone']['retired']}
    if not objective or not objective['endpoint'] or not active or path not in report['objective_paths']:
        raise DomainError('milestone_contract_incomplete', 'An objective needs active milestones and a compiled endpoint', 409)
    expected = set(objective['endpoint']['declarations'])
    references = tables['reference']
    for key, node in active.items():
        m = node['milestone']
        if m['statement'] != 'accepted' or not m['contract']:
            raise DomainError('milestone_contract_incomplete', f'{key} has no accepted contract', 409)
        expected.update(m['contract']['declarations'])
        for citation in m['references']:
            if not conn.execute(select(references.c.id).where(references.c.project_id == project_id,
                    references.c.cite_key == citation['cite_key'], references.c.status == 'active')).first():
                raise DomainError('milestone_reference_missing', f"Register complete bibliography for {citation['cite_key']}", 409)
    if not expected <= set(report['targets']) or not expected <= set(report['types']):
        raise DomainError('milestone_contract_incomplete', 'The check omitted declared milestone or endpoint targets', 409)
    contract = accepted_gate(conn, data.contract_gate_id, receipt['repository_id'], report, 'contract')
    gate_policy = get(conn, 'review_policy', get(conn, 'review_gate', data.contract_gate_id)['policy_id'])
    if data.route_gate_id:
        route = accepted_gate(conn, data.route_gate_id, receipt['repository_id'], report, 'route')
    elif gate_policy.get('specialist_mode') == 'advisory':
        route = contract
    else:
        raise DomainError('milestone_review_missing', 'This policy requires a separately accepted route', 409)
    if not set(active) <= set(route['report']['milestone_keys']) or contract['report']['manifest_digest'] != manifest['digest']:
        raise DomainError('milestone_review_stale', 'Review the complete route and integrated contract before freezing', 409)
    if data.previous_snapshot_id:
        previous = same_project(conn, 'roadmap_snapshot', data.previous_snapshot_id, project_id)
        require_baseline(conn, previous)
        if previous['roadmap_document_id'] != document['id']:
            raise DomainError('invalid_baseline', 'Corrections must retain the same objective', 422)
        old = get(conn, 'artifact', previous['graph_manifest_artifact_id'])['content']
        # Human intent changes require a new human decision; maintainers may correct contracts.
        old_manifest = json.loads(service.store.read(old['sha256'], old['size_bytes']))
        if actor.kind != 'human' and not automatic and old_manifest['objectives'][path]['body'] != objective['body']:
            raise DomainError('human_approval_required', 'Changed objective text requires human approval', 403)
    now = datetime.now(timezone.utc)
    evidence = {'principal_id': str(actor.id), 'kind': actor.kind, 'check_id': str(receipt['id']),
                'route_gate_id': str(data.route_gate_id), 'contract_gate_id': str(data.contract_gate_id),
                'previous_snapshot_id': str(data.previous_snapshot_id) if data.previous_snapshot_id else None,
                'note': data.note, 'approved_at': now.isoformat(), 'manifest_digest': manifest['digest']}
    artifacts = [create(conn, 'artifact', project_id=project_id, kind='blob', content=service.store.put_json(value))
                 for value in (manifest, evidence)]
    baseline = create(conn, 'roadmap_snapshot', project_id=project_id, roadmap_document_id=document['id'],
        source_commit_oid=receipt['source_commit_oid'], graph_manifest_artifact_id=artifacts[0]['id'],
        acceptance_artifact_id=artifacts[1]['id'], status='frozen', frozen_at=now)
    create(conn, 'milestone_acceptance', project_id=project_id, snapshot_id=baseline['id'], principal_id=actor.id,
        check_id=receipt['id'], previous_snapshot_id=data.previous_snapshot_id, note=data.note)
    emit(conn, actor.id, project_id, 'roadmap_snapshot', baseline, ['status'])
    return baseline


def require_baseline(conn, baseline):
    if not enabled(conn, baseline['project_id']):
        return
    acceptance = tables['milestone_acceptance']
    if baseline['status'] != 'frozen' or not conn.execute(select(acceptance.c.id).where(
            acceptance.c.snapshot_id == baseline['id'])).first():
        raise DomainError('milestone_approval_required', 'Formalization requires an approved, verified frozen milestone baseline', 409)


def _initial_baseline_readiness(conn, document, manifest, baselines, reviewed_contract):
    result = {
        'scope': 'initial_baseline_indexed_source_verification',
        'approval_readiness_evaluated': False,
        'approval_requires_human': True,
        'initial_baseline_accepted': bool(baselines),
        'accepted_contract_observed': reviewed_contract,
        'required_source_commit_oid': document['source_commit_oid'],
        'required_manifest_digest': manifest['digest'],
        'verification_status': 'not_required' if baselines else 'missing',
        'check_id': None, 'blockers': [], 'next_actions': [],
        'note': 'This diagnoses indexed-source verification only; all contract, citation and review requirements '
                'are still checked by baseline acceptance. Initial human approval and launching formalization '
                'are separate from completing the preprocessing packet.',
    }
    if baselines:
        return result
    check = tables['milestone_check']
    matched = conn.execute(select(check.c.id).where(
        check.c.repository_id == document['source_repository_id'],
        check.c.source_commit_oid == document['source_commit_oid'],
        check.c.kind.in_(['graph', 'contract']), check.c.report['compiled'].as_boolean().is_(True),
        check.c.report['manifest_digest'].astext == manifest['digest'])
        .order_by(check.c.created_at.desc(), check.c.id.desc()).limit(1)).scalar_one_or_none()
    if matched:
        result.update(verification_status='available', check_id=str(matched))
    else:
        result['blockers'].append({
            'code': 'indexed_source_check_missing',
            'reason': 'No successful graph or contract receipt covers this exact indexed source and manifest. '
                      'A receipt for the pre-merge PR head does not verify the merged source commit.',
        })
    if not reviewed_contract:
        result['next_actions'].append({
            'action': 'complete_contract_review',
            'reason': 'Finish and merge the applicable route and integrated contract reviews before requesting '
                      'final merged-source verification for the approval packet.',
        })
    elif not matched:
        result['next_actions'].append({
            'action': 'verify_indexed_source', 'method': 'POST', 'path': '/api/v3/milestones/verifications',
            'repository_id': str(document['source_repository_id']),
            'schema_lookup': {'section': 'operations', 'name': 'milestone_verification'},
            'schema_command': 'horizon-pipeline agent schema --section operations --name milestone_verification',
            'request_template': {'source_commit_oid': document['source_commit_oid']},
            'required_inputs': ['workspace_id', 'base_commit_oid'],
            'reason': 'Select a registered ready roadmap workspace for this repository and the exact comparison '
                      'base. For unchanged merged contracts, use the reviewed contract head as the base. '
                      'Reuse a matching existing job; consume its successful receipt before publishing the final packet.',
        })
    return result


def objective_view(conn, actor, document_id, config):
    document = get(conn, 'document', document_id)
    require_project(conn, actor, document['project_id'])
    manifest = projected_manifest(conn, document['source_repository_id'], document['source_commit_oid'])
    from ..integrations.integration_views import repository_url
    repository = get(conn, 'repository', document['source_repository_id'])
    source_base = repository_url(repository, get(conn, 'integration', repository['integration_id']), config)
    check = tables['milestone_check']
    receipts = list(conn.execute(select(check).where(check.c.repository_id == document['source_repository_id'],
        check.c.source_commit_oid == document['source_commit_oid']).order_by(check.c.created_at.desc()).limit(100)).mappings())
    nodes = []
    for key, node in manifest['nodes'].items():
        m = node['milestone']
        if not m or m['objective'] != document['source_path']:
            continue
        proof = 'open' if m['proof'] in {'conditional', 'complete'} else m['proof']
        if m['proof_check_id']:
            row = conn.execute(select(check).where(check.c.id == UUID(m['proof_check_id']),
                check.c.repository_id == document['source_repository_id'], check.c.kind == 'proof')).mappings().first()
            proof = 'needs_recheck'
            if row and row['report']['manifest_digest'] == manifest['digest'] and m['contract']:
                axioms = row['report']['targets']
                names = m['contract']['declarations']
                if all(name in axioms for name in names):
                    if set(names) & set(row['report'].get('direct_admissions', [])):
                        proof = 'in_progress' if m['proof'] == 'in_progress' else 'open'
                    else:
                        proof = 'conditional' if any('sorryAx' in axioms[name] for name in names) else 'complete'
        source_url = (f"{source_base}/src/commit/{quote(document['source_commit_oid'], safe='')}/"
                      f"{quote(m['contract']['path'], safe='/')}" if source_base and m['contract'] else None)
        nodes.append({**node, 'key': key, 'proof_status': proof, 'statement_claim': m['statement'], 'source_url': source_url})
    snapshots = tables['roadmap_snapshot']
    baselines = list(conn.execute(select(snapshots).join(tables['milestone_acceptance'],
        tables['milestone_acceptance'].c.snapshot_id == snapshots.c.id).where(snapshots.c.roadmap_document_id == document_id)
        .order_by(snapshots.c.created_at.desc()).limit(50)).mappings())
    acceptance = tables['milestone_acceptance']
    current = conn.execute(select(snapshots.c.id).join(acceptance, acceptance.c.snapshot_id == snapshots.c.id)
        .join(check, check.c.id == acceptance.c.check_id).where(snapshots.c.roadmap_document_id == document_id,
            check.c.report['manifest_digest'].astext == manifest['digest'])
        .order_by(snapshots.c.created_at.desc()).limit(1)).scalar_one_or_none()
    gates = tables['review_gate']
    item = tables['forge_item']
    choices = []
    reviewed_contract = bool(current)
    for row in conn.execute(select(gates, item.c.title, item.c.head_commit_oid, item.c.remote_number).join(item).where(
            item.c.repository_id == document['source_repository_id'], item.c.status == 'merged',
            gates.c.status == 'accepted').order_by(gates.c.created_at.desc()).limit(100)).mappings():
        c = conn.execute(select(check).where(check.c.repository_id == document['source_repository_id'],
            check.c.source_commit_oid == row['head_commit_oid']).order_by(check.c.created_at.desc()).limit(1)).mappings().first()
        if c:
            choices.append({'id': row['id'], 'title': row['title'], 'kind': c['kind'],
                'url': f"{source_base}/pulls/{row['remote_number']}" if source_base else None})
            if c['kind'] == 'contract' and c['report']['manifest_digest'] == manifest['digest']:
                reviewed_contract = True
    for node in nodes:
        node['statement_status'] = ('needs_revision' if node['statement_claim'] == 'needs_revision'
            else 'accepted' if reviewed_contract and node['milestone']['contract'] else 'proposed')
    return json_value({'document': document, 'nodes': nodes, 'manifest_digest': manifest['digest'],
        'current_baseline_id': current,
        'initial_baseline_readiness': _initial_baseline_readiness(conn, document, manifest, baselines, reviewed_contract),
        'checks': [{'id': r['id'], 'kind': r['kind'], 'source_commit_oid': r['source_commit_oid']} for r in receipts],
        'gates': choices, 'baselines': baselines, 'enabled': enabled(conn, document['project_id'])})
