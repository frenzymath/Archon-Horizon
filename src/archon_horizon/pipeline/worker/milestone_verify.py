"""Compatibility verifier for explicitly retained milestone-workflow projects.

New graph workflows do not call this parser or require milestone receipts.
General Lean/library audits live in lean_verify and have no planning policy.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from ..projects.milestone_sources import source_manifest, milestone_table, digest
from . import build_engine
from .lean_build import LeanBuildPolicy, check
from .lean_verify import (FOUNDATIONS, audit, audit_imports, clean_commit, git, library_tree, verify_library)


def sources_at(root, revision):
    # Keep this worker helper independent of server/database dependencies.
    result = {}
    total = 0
    for entry in git(root, 'ls-tree', '-rlz', revision).split('\0'):
        if not entry:
            continue
        info, path = entry.split('\t', 1)
        mode, kind, oid, size = info.split()
        if not (path.endswith('.lean') or path.startswith(('nodes/', 'objectives/', 'milestones/')) and path.endswith('.md')
                or path in {'lean-toolchain', 'lakefile.toml', 'lake-manifest.json'}):
            continue
        total += int(size)
        if (mode not in {'100644', '100755'} or kind != 'blob' or int(size) > 2 * 1024**2
                or total > 32 * 1024**2 or len(result) >= 10000):
            raise ValueError('Roadmap source exceeds the regular-file verification bounds')
        result[path] = subprocess.run(['git', '-C', str(root), 'cat-file', 'blob', oid],
            check=True, capture_output=True, timeout=30).stdout.decode()
    return result


def verify(root, base, policy, *, solution_root=None, comparator_config=None):
    root = root.resolve(strict=True)
    commit = clean_commit(root)
    base = git(root, 'rev-parse', '--verify', '--end-of-options', base + '^{commit}')
    git(root, 'merge-base', '--is-ancestor', base, commit)
    sources = sources_at(root, commit)
    manifest, before = source_manifest(sources), source_manifest(sources_at(root, base))
    identities = {(n['milestone']['objective'], n['milestone'].get('namespace', 'formalization'), n['milestone']['id']): n['milestone']
                  for n in manifest['nodes'].values() if n['milestone']}
    for node in before['nodes'].values():
        old = node['milestone']
        if old:
            current = identities.get((old['objective'], old.get('namespace', 'formalization'), old['id']))
            if not current or old['retired'] and not current['retired']:
                raise ValueError('Keep retired milestone identities; do not delete or reuse their IDs')
    if not manifest['objectives']:
        raise ValueError('Add objective milestones metadata and milestone nodes before checking a route')
    nodes = {k: n for k, n in manifest['nodes'].items() if n['milestone'] and not n['milestone']['retired']}
    if not nodes:
        raise ValueError('The route has no active milestones')
    if any(n['milestone']['proof'] in {'conditional', 'complete'} and not n['milestone']['contract'] for n in nodes.values()):
        raise ValueError('Proof claims require public target locators')
    locators = [n['milestone']['contract'] for n in nodes.values() if n['milestone']['contract']]
    locators += [o['endpoint'] for o in manifest['objectives'].values() if o['endpoint']]
    def contract_surface(value):
        return {'inputs': value['inputs'],
            'nodes': {k: {**n, 'milestone': {field: v for field, v in n['milestone'].items()
                      if field not in {'statement', 'proof', 'proof_check_id'}}}
                      for k, n in value['nodes'].items() if n['milestone'] and n['milestone']['contract']},
            'endpoints': {k: o for k, o in value['objectives'].items() if o['endpoint']}}
    route_only = not locators or contract_surface(manifest) == contract_surface(before)
    kind = 'graph' if manifest['digest'] == before['digest'] else 'route' if route_only else 'contract'
    for path, objective in manifest['objectives'].items():
        table_path = objective['root'] + '/milestones.md'
        table = sources.get(table_path, '')
        if milestone_table(manifest, path).strip() not in table:
            raise ValueError(f'Update the generated Statement/Proof table in {table_path} '
                f'(objective: {path}) before verification. Render committed source with '
                'python -m archon_horizon.pipeline.worker.milestone_verify --root <checkout> --tables, '
                'include the generated table in the source PR, then verify its new head.')
    definitions = sorted({path.removeprefix('milestones/').removesuffix('.lean').replace('/', '.')
        for path in sources if path.startswith('milestones/') and '/Definitions/' in path and path.endswith('.lean')})
    checked = {'targets': {}, 'definitions': {}, 'types': {}, 'declaration_modules': {}, 'direct_admissions': []}
    if locators:
        for required in ('lean-toolchain', 'lake-manifest.json'):
            if required not in sources:
                raise ValueError(f'Pin {required} before reviewing contracts')
        if definitions:
            audit(root, [], definitions, policy, forbidden_modules=[item['module'] for item in locators])
        checked = audit(root, locators, definitions, policy)
        for locator in locators:
            if any(checked['declaration_modules'][name] != locator['module'] for name in locator['declarations']):
                raise ValueError('A target declaration does not belong to its declared module')
    report = {'schema_version': 1, 'source_commit_oid': commit, 'base_commit_oid': base,
        'kind': kind, 'manifest_digest': manifest['digest'], 'base_manifest_digest': before['digest'],
        'ancestor_commits': git(root, 'rev-list', '--max-count=10000', commit).splitlines(),
        'milestone_keys': sorted(nodes), 'objective_paths': sorted(manifest['objectives']),
        'proof_claims': {key: {'status': n['milestone']['proof'], 'check_id': n['milestone']['proof_check_id'],
            'declarations': n['milestone']['contract']['declarations']} for key, n in nodes.items()
            if n['milestone']['proof'] in {'conditional', 'complete'} and n['milestone']['contract']},
        'compiled': True, 'toolchain': sources.get('lean-toolchain', 'route-only').strip(), **checked,
        'implementation_commit_oid': None, 'comparator_passed': False}
    if solution_root:
        solution_root = solution_root.resolve(strict=True)
        solution_commit = clean_commit(solution_root)
        if not comparator_config or not locators:
            raise ValueError('Proof checks require a comparator configuration and accepted contract locators')
        config = json.loads(comparator_config.read_text())
        targets = sorted(checked['targets'])
        if sorted(config.get('theorem_names', [])) != targets:
            raise ValueError('Comparator must cover every milestone and endpoint target')
        if set(config.get('permitted_axioms', [])) - FOUNDATIONS - {'sorryAx'}:
            raise ValueError('Comparator permits nonstandard axioms')
        if not set(checked['definitions']) <= set(config.get('definition_names', [])):
            raise ValueError('Comparator must compare the complete reviewed definition surface')
        # The challenge modules must be the exact reviewed source, including shared definitions.
        for path, content in sources.items():
            if path.startswith('milestones/') and path.endswith('.lean') or path == 'lean-toolchain':
                if (solution_root / path).read_text() != content:
                    raise ValueError('Comparator challenge differs from the pinned roadmap source')
        from pydantic import TypeAdapter
        from ..projects.milestone_sources import LeanName
        module = TypeAdapter(LeanName).validate_python(config['solution_module'])
        challenge = TypeAdapter(LeanName).validate_python(config['challenge_module'])
        if challenge == module:
            raise ValueError('Challenge and solution environments must be distinct')
        dependencies = json.loads(sources['lake-manifest.json']).get('packages', [])
        implementation_dependencies = {p['name']: p for p in
            json.loads((solution_root / 'lake-manifest.json').read_text()).get('packages', [])}
        if any(implementation_dependencies.get(p['name']) != p for p in dependencies):
            raise ValueError('Challenge dependency pins differ from the reviewed package')
        challenge_audit = audit(solution_root, locators, definitions, policy, modules=[challenge])
        if (challenge_audit['declaration_modules'] != checked['declaration_modules']
                or challenge_audit['definitions'] != checked['definitions']):
            raise ValueError('Comparator challenge does not expose the reviewed declarations and definitions')
        build = check(solution_root, ['@Comparator/comparator', '@lean4export/lean4export'], policy)
        if not build['ok']:
            raise ValueError('Pinned comparator tools did not build')
        executable = solution_root / '.lake/packages/Comparator/.lake/build/bin/comparator'
        completed = build_engine.captured_command(['lake', 'env', str(executable), str(comparator_config.resolve())],
            solution_root, os.environ, time.monotonic() + policy.timeout_seconds)
        if completed.returncode:
            raise ValueError('Comparator rejected the implementation: ' + completed.stdout.decode(errors='replace')[-4000:])
        implementation = audit(solution_root, locators, [], policy, modules=[module])
        report.update(implementation_commit_oid=solution_commit, comparator_passed=True, kind='proof',
                      targets=implementation['targets'], types=implementation['types'],
                      direct_admissions=implementation['direct_admissions'])
        if clean_commit(solution_root) != solution_commit:
            raise ValueError('Implementation changed during verification')
    if clean_commit(root) != commit or source_manifest(sources_at(root, commit))['digest'] != manifest['digest']:
        raise ValueError('Roadmap changed during verification')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--base', help='Exact current PR target commit')
    parser.add_argument('--worker-config', type=Path)
    parser.add_argument('--workspace-id')
    parser.add_argument('--solution-root', type=Path)
    parser.add_argument('--comparator-config', type=Path)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--tables', action='store_true', help='Print generated milestone tables for the source PR')
    args = parser.parse_args()
    if args.tables:
        manifest = source_manifest(sources_at(args.root, 'HEAD'))
        for objective in manifest['objectives']:
            print(objective + '\n' + milestone_table(manifest, objective))
        return
    if not args.base or not args.worker_config or args.publish and not args.workspace_id:
        parser.error('Verification requires --base and --worker-config; publishing also requires --workspace-id')
    from ..worker_config import WorkerConfig, private_text
    config = WorkerConfig.model_validate_json(args.worker_config.read_bytes())
    if not config.lean_build or not any(args.root.resolve().is_relative_to(p.resolve()) for p in config.workspace_roots):
        raise ValueError('Select a configured worker workspace and managed Lean build policy')
    report = verify(args.root, args.base, LeanBuildPolicy(**config.lean_build.model_dump()),
                    solution_root=args.solution_root, comparator_config=args.comparator_config)
    if args.publish:
        import httpx
        payload = {'workspace_id': args.workspace_id, 'report': report}
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.post(config.api_url + '/api/v3/worker/milestone-checks', json=payload,
                headers={'Authorization': 'Bearer ' + private_text(config.token_file), 'Idempotency-Key': digest(payload)})
            response.raise_for_status()
            print(json.dumps(response.json(), indent=2))
    else:
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
