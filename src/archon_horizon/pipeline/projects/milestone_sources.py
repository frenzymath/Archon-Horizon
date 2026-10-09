"""Portable source contracts. Parsing never executes repository code."""

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .documents import parse_document
from ..models import Contract, RelativePath, Slug, Text

LeanName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_']*(\.[A-Za-z_][A-Za-z0-9_']*)*$", max_length=512)]


class Declaration(Contract):
    path: RelativePath
    module: LeanName
    declarations: list[LeanName] = Field(min_length=1, max_length=100)


class Citation(Contract):
    cite_key: Slug
    locator: Text


class ObjectiveMilestones(Contract):
    root: RelativePath
    endpoint: Declaration | None = None


class Milestone(Contract):
    objective: RelativePath
    namespace: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")] = "formalization"
    provenance: list[str] = Field(default_factory=list, max_length=100)
    id: Annotated[str, Field(pattern=r"^M[0-9]{2,4}$")]
    contract: Declaration | None = None
    definitions: list[RelativePath] = Field(default_factory=list, max_length=200)
    references: list[Citation] = Field(default_factory=list, max_length=100)
    statement: Literal["proposed", "accepted", "needs_revision"] = "proposed"
    proof: Literal["open", "in_progress", "conditional", "complete", "needs_recheck"] = "open"
    proof_check_id: UUID | None = None
    retired: bool = False

    @model_validator(mode="after")
    def accepted_contract(self):
        if self.statement == "accepted" and (not self.contract or not self.references):
            raise ValueError("Accepted milestone claims require a Lean contract and precise references")
        return self


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def validate_edges(nodes, field):
    remaining = {key: len(set(node[field])) for key, node in nodes.items()}
    consumers = {key: [] for key in nodes}
    for key, node in nodes.items():
        for target in set(node[field]):
            if target not in nodes:
                raise ValueError(f'{field} references missing node {target}')
            consumers[target].append(key)
    ready = [key for key, count in remaining.items() if not count]
    visited = 0
    while ready:
        visited += 1
        for consumer in consumers[ready.pop()]:
            remaining[consumer] -= 1
            if not remaining[consumer]:
                ready.append(consumer)
    if visited != len(nodes):
        raise ValueError(f'{field} must be acyclic')


def source_manifest(sources: dict[str, str]) -> dict:
    objectives, nodes, identities = {}, {}, set()
    for path, content in sources.items():
        if not path.endswith('.md') or not path.startswith(('nodes/', 'objectives/')):
            continue
        metadata, body = parse_document(content)
        if path.startswith('objectives/') and 'milestones' in metadata:
            spec = ObjectiveMilestones.model_validate(metadata['milestones'])
            if not spec.root.startswith('milestones/'):
                raise ValueError('Milestone roots must be under milestones/')
            objectives[path] = {'title': metadata.get('title', PurePosixPath(path).stem),
                                'body': body, **spec.model_dump(mode='json')}
        elif path.startswith('nodes/'):
            key = metadata.get('label') or metadata.get('id') or PurePosixPath(path).stem
            if not isinstance(key, str) or key in nodes:
                raise ValueError('Node identities must be unique strings')
            owned = metadata.get('belongs_to', [])
            children = metadata.get('children', [])
            if any(not isinstance(values, list) or not all(isinstance(v, str) for v in values)
                   for values in (owned, children)):
                raise ValueError('belongs_to and children must be lists of node keys')
            milestone = Milestone.model_validate(metadata['milestone']) if 'milestone' in metadata else None
            nodes[key] = {'path': path, 'title': metadata.get('title', key), 'children': children,
                          'belongs_to': owned, 'milestone': milestone.model_dump(mode='json', exclude={'namespace', 'provenance'}) if milestone else None}
            if milestone and (milestone.namespace != 'formalization' or milestone.provenance):
                nodes[key]['milestone'].update(namespace=milestone.namespace, provenance=milestone.provenance)
    roots = [obj['root'] for obj in objectives.values()]
    # Legacy graphs retain their existing indexer diagnostics.
    if objectives:
        validate_edges(nodes, 'children')
        validate_edges(nodes, 'belongs_to')
    if len(roots) != len(set(roots)):
        raise ValueError('Each objective requires a distinct milestone root')
    locators = []
    for key, node in nodes.items():
        m = node['milestone']
        for owner in node['belongs_to']:
            if owner == key or owner not in nodes or not nodes[owner]['milestone']:
                raise ValueError('belongs_to must name a different existing milestone node')
        if not m:
            continue
        if any(source == key or source not in nodes for source in m.get('provenance', [])):
            raise ValueError('Milestone provenance must link other existing nodes')
        identity = (m['objective'], m.get('namespace', 'formalization'), m['id'])
        if identity in identities:
            raise ValueError('Duplicate milestone ID within an objective')
        identities.add(identity)
        if m['objective'] not in objectives:
            raise ValueError('Milestone objective is absent or has no milestones configuration')
        root = objectives[m['objective']]['root']
        if m['contract']:
            if not m['contract']['path'].startswith(root + '/'):
                raise ValueError('Milestone contract must be inside its objective root')
            locators.append(m['contract'])
        for path in m['definitions']:
            if not path.startswith('milestones/') or '/Definitions/' not in path or not path.endswith('.lean') or path not in sources:
                raise ValueError('Definition locators require existing milestones/.../Definitions/*.lean files')
    for obj in objectives.values():
        if obj['endpoint']:
            if not obj['endpoint']['path'].startswith(obj['root'] + '/'):
                raise ValueError('Endpoint must be inside its objective root')
            locators.append(obj['endpoint'])
    declarations = set()
    for locator in locators:
        path = locator['path']
        if not path.endswith('.lean') or path not in sources:
            raise ValueError(f'Lean source is missing: {path}')
        if locator['module'] != path.removeprefix('milestones/').removesuffix('.lean').replace('/', '.'):
            raise ValueError('Lean modules must match their path relative to milestones/')
        for name in locator['declarations']:
            if name in declarations:
                raise ValueError('Each public target declaration must have one locator')
            declarations.add(name)
    # Freeze all Lean inputs, including shared definitions; generated status tables are excluded.
    inputs = {path: hashlib.sha256(text.encode()).hexdigest() for path, text in sources.items()
              if path.endswith('.lean') or path in {'lean-toolchain', 'lakefile.toml', 'lake-manifest.json'}}
    contract_nodes = {key: {**node, 'children': [child for child in node['children']
        if child in nodes and nodes[child]['milestone']], 'milestone': ({k: v for k, v in node['milestone'].items()
        if k not in {'statement', 'proof', 'proof_check_id'}} if node['milestone'] else None)}
        for key, node in nodes.items() if node['milestone']}
    frozen_objectives = {path: {**obj, 'body': re.sub(r'(?m)^(\s*[-*+] )\[[xX ]\]', r'\1[ ]', obj['body'])}
                         for path, obj in objectives.items()}
    contract = {'objectives': frozen_objectives, 'nodes': contract_nodes, 'inputs': inputs}
    return {**contract, 'nodes': nodes, 'digest': digest(contract)}


def milestone_table(manifest, objective):
    rows = ['| Milestone | Statement | Proof |', '| --- | --- | --- |']
    for key, node in sorted(manifest['nodes'].items()):
        m = node['milestone']
        if m and m['objective'] == objective and not m['retired']:
            title = str(node['title']).replace('|', '\\|').replace('\n', ' ')
            identity = m['id'] if m.get('namespace', 'formalization') == 'formalization' else m['namespace'] + '/' + m['id']
            rows.append(f"| {identity}: {title} | {m['statement']} | {m['proof']} |")
    return '\n'.join(rows) + '\n'
