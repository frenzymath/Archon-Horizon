"""Exact-source Lean builds and axiom audits, independent of graph planning.

Roadmap milestones are agent-authored strategy. These helpers instead verify
committed Lean contributions, including every declaration in changed modules
and their transitive axiom dependencies. Legacy milestone checks reuse the
same compiler machinery without making it part of normal planning policy.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from . import build_engine
from .lean_build import check


def digest(value):
    """Stable receipt hash over canonical UTF-8 JSON, not working-tree mtimes."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


FOUNDATIONS = {'propext', 'Classical.choice', 'Quot.sound'}



AUDIT = r'''
open Lean Elab Command in
run_cmd do
  let targets : Array String := __TARGETS__
  let definitionModules : Array String := __DEFINITIONS__
  let forbiddenModules : Array String := __FORBIDDEN__
  let env <- getEnv
  for mod in env.header.moduleNames do
    if forbiddenModules.contains mod.toString then
      throwError "Definition modules import a milestone contract: {mod}"
  let mut targetRows := []
  let mut typeRows := []
  let mut moduleRows := []
  let mut directAdmissions : Array String := #[]
  for text in targets do
    let name := text.toName
    let info <- getConstInfo name
    unless info matches .thmInfo _ do
      throwError "Milestone targets must be theorems: {name}"
    let axs <- collectAxioms name
    if ((info.value? true).getD (.sort .zero)).getUsedConstants.contains ``sorryAx then
      directAdmissions := directAdmissions.push text
    targetRows := (text, toJson (axs.map Name.toString)) :: targetRows
    let mut typeAxioms : Array Name := #[]
    for used in info.type.getUsedConstants do
      typeAxioms := typeAxioms ++ (<- collectAxioms used)
    typeRows := (text, toJson (typeAxioms.map Name.toString)) :: typeRows
    moduleRows := (text, toJson ((<- findModuleOf? name).map Name.toString |>.getD "")) :: moduleRows
  let mut definitionRows := []
  for (name, _) in env.constants.toList do
    let mod := (<- findModuleOf? name).map Name.toString |>.getD ""
    if definitionModules.contains mod then
      definitionRows := (name.toString, toJson ((<- collectAxioms name).map Name.toString)) :: definitionRows
  let result := Json.mkObj [("targets", Json.mkObj targetRows), ("types", Json.mkObj typeRows),
    ("definitions", Json.mkObj definitionRows), ("declaration_modules", Json.mkObj moduleRows),
    ("direct_admissions", toJson directAdmissions)]
  liftIO <| IO.println ("HORIZON_MILESTONE_AUDIT=" ++ result.compress)
'''



def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, timeout=30).stdout.decode().strip()



def clean_commit(root):
    if git(root, 'status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('Milestone checks require a clean committed checkout')
    return git(root, 'rev-parse', 'HEAD')



def audit_imports(root, environment, deadline):
    """Use the audit's Lean APIs without loading unrelated compiler modules.

    The split evaluator module is unavailable in older toolchains. Detect it
    in the project's actual Lean installation and retain the full Lean import
    there; module availability must not be guessed from a version string.
    """
    result = build_engine.captured_command(['lake', 'env', 'lean', '--print-prefix'], root, environment, deadline)
    prefix = Path(result.stdout.decode().strip())
    modules = ('Lean.Elab.BuiltinEvalCommand', 'Lean.Util.CollectAxioms')
    if result.returncode == 0 and prefix.is_absolute() and all(
            (prefix / 'lib/lean' / (name.replace('.', '/') + '.olean')).is_file() for name in modules):
        return modules
    return ('Lean',)



def audit(root, locators, definitions, policy, *, modules=None, forbidden_modules=()):
    targets = sorted({name for item in locators for name in item['declarations']})
    imports = sorted(set(modules) if modules is not None else
                     set(item['module'] for item in locators) | set(definitions))
    result = check(root, imports, policy)
    if not result['ok'] or not result.get('snapshot_verified'):
        raise ValueError('Lean milestone build did not pass: ' + json.dumps(result))
    policy.root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + policy.timeout_seconds
    environment = {**os.environ, 'HORIZON_LEAN_CACHE_ROOT': str(policy.root),
                   'HORIZON_BUILD_SLOTS': str(policy.max_parallel_builds)}
    # A small native probe used roughly 500 MiB with these APIs, versus 880 MiB
    # for `import Lean` on Lean 4.33.1. This is an observation, not a RAM limit.
    required = audit_imports(root, environment, deadline)
    with tempfile.TemporaryDirectory(prefix='milestone-audit-', dir=policy.root) as scratch:
        probe = Path(scratch) / 'Audit.lean'
        array = lambda values: '#[' + ', '.join(json.dumps(value) for value in values) + ']'
        probe.write_text(''.join(f'import {module}\n' for module in (*required, *imports)) +
            AUDIT.replace('__TARGETS__', array(targets)).replace('__DEFINITIONS__', array(definitions))
            .replace('__FORBIDDEN__', array(forbidden_modules)))
        progress = build_engine.CheckProgress(policy.queue_timeout_seconds)
        with build_engine.resource_lock(build_engine.checkout_paths(root), deadline,
                progress), build_engine.build_slot(environment, deadline, progress):
            completed = build_engine.captured_command(['lake', 'env', 'lean', str(probe)], root, environment, deadline)
        if completed.returncode:
            raise ValueError('Lean milestone audit failed: ' + completed.stdout.decode(errors='replace')[-6000:])
        rows = [line.split('=', 1)[1] for line in completed.stdout.decode().splitlines()
                if line.startswith('HORIZON_MILESTONE_AUDIT=')]
        if len(rows) != 1:
            raise ValueError('Lean did not produce one complete structured audit')
        checked = json.loads(rows[0])
    if set(checked['targets']) != set(targets):
        raise ValueError('Lean audit omitted target declarations')
    for axioms in [*checked['definitions'].values(), *checked['types'].values()]:
        if set(axioms) - FOUNDATIONS:
            raise ValueError('Admitted or nonstandard axioms in milestone definitions or statement types')
    for axioms in checked['targets'].values():
        if set(axioms) - FOUNDATIONS - {'sorryAx'}:
            raise ValueError('Milestone theorem uses a nonstandard axiom')
    return checked



def library_tree(root, revision):
    """Hash Git object identities without reading an entire destination library."""
    result = {}
    for entry in git(root, 'ls-tree', '-rz', revision).split('\0'):
        if not entry:
            continue
        info, path = entry.split('\t', 1)
        mode, kind, oid = info.split()
        if path.endswith('.lean') or path in {'lean-toolchain', 'lakefile.toml', 'lake-manifest.json'}:
            if mode not in {'100644', '100755'} or kind != 'blob':
                raise ValueError('Library sources must be regular committed files')
            result[path] = oid
    return result



def verify_library(root, base, policy):
    """Audit all declarations in changed modules and their full axiom closure.

    The host derives the module set from the exact Git base/head. Requesters
    cannot cherry-pick safe targets. Changes to build/dependency inputs expand
    verification to all committed Lean modules. Unsupported module layouts fail
    import rather than silently dropping files from the verification scope.
    """
    root = root.resolve(strict=True)
    commit = clean_commit(root)
    base = git(root, 'rev-parse', '--verify', '--end-of-options', base + '^{commit}')
    git(root, 'merge-base', '--is-ancestor', base, commit)
    sources, before = library_tree(root, commit), library_tree(root, base)
    if not sources.get('lean-toolchain') or not sources.get('lake-manifest.json'):
        raise ValueError('Pin the library toolchain and dependency manifest')
    pins = {'lean-toolchain', 'lake-manifest.json', 'lakefile.lean', 'lakefile.toml'}
    changed_pins = any(sources.get(path) != before.get(path) for path in pins)
    modules = sorted(path[:-5].replace('/', '.') for path in sources
                     if path.endswith('.lean') and path != 'lakefile.lean'
                     and (changed_pins or sources[path] != before.get(path)))
    checked = audit(root, [], modules, policy)
    toolchain = git(root, 'show', commit + ':lean-toolchain')
    # Compilation and the final audit are separate subprocesses. Attribute the
    # receipt only if neither phase changed the source or the checked Git head.
    if clean_commit(root) != commit:
        raise ValueError('Library changed during verification')
    return {'schema_version': 1, 'kind': 'library', 'source_commit_oid': commit,
        'base_commit_oid': base, 'manifest_digest': digest(sources),
        'base_manifest_digest': digest(before), 'compiled': True,
        'ancestor_commits': git(root, 'rev-list', '--max-count=10000', commit).splitlines(),
        'milestone_keys': [], 'objective_paths': [], 'toolchain': toolchain,
        **checked, 'implementation_commit_oid': None, 'comparator_passed': False}
