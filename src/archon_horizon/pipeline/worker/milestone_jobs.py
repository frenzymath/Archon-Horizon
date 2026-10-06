"""Host-owned milestone checker lane, with isolated checkouts and leased jobs."""

from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import httpx

from .lean_build import LeanBuildPolicy
from .milestone_verify import git, verify


def checkout(workspace, commit, destination, roots, remotes, headers):
    source = Path(workspace['path']).resolve(strict=True)
    if not any(source.is_relative_to(root) for root in roots):
        raise ValueError('Verification workspace is outside configured roots')
    subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(source), str(destination)],
                   check=True, capture_output=True, timeout=120)
    ensure_commit(workspace, commit, destination, remotes, headers)
    git(destination, 'checkout', '--quiet', '--detach', commit)
    # This private clone owns Lake's generated output, even when the repository
    # has no .gitignore. Git still reports every tracked change, including files
    # beneath .lake; source files elsewhere retain the normal cleanliness guard.
    exclude = destination / '.git' / 'info' / 'exclude'
    with exclude.open('a') as output:
        output.write('\n/.lake/\n')


def ensure_commit(workspace, commit, root, remotes, headers):
    try:
        git(root, 'cat-file', '-e', commit + '^{commit}')
        return
    except subprocess.CalledProcessError:
        pass
    repository_id = str(workspace['repository_id'])
    remote = remotes.get(repository_id)
    if not remote:
        raise ValueError('Commit absent locally; configure publication_remotes for the verification repository')
    environment = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    if headers.get(repository_id):
        environment.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.extraHeader',
                           GIT_CONFIG_VALUE_0=headers[repository_id])
    subprocess.run(['git', '-C', str(root), 'fetch', '--quiet', '--no-tags', '--', remote, commit],
                   env=environment, check=True, capture_output=True, timeout=120)


def stop_process(process):
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def execute(daemon, job, stop):
    harness = next(iter(daemon.harnesses.values()))
    policy = harness.lean_build
    policy.root.mkdir(parents=True, exist_ok=True)
    request = job['request']
    lease = {'claim_token': job['claim_token']}
    path = '/api/v3/worker/milestone-jobs/' + job['id']
    heartbeat_at = 0.0
    def renew():
        nonlocal heartbeat_at
        response = daemon.transport._post(path + '/heartbeat', lease)
        response.raise_for_status()
        heartbeat_at = time.monotonic()

    with tempfile.TemporaryDirectory(prefix='milestone-job-', dir=policy.root) as temporary:
        scratch = Path(temporary)
        inputs = {'policy': asdict(policy), 'root': str(scratch / 'roadmap'), 'base': request['base_commit_oid']}
        renew()
        checkout(job['workspace'], request['source_commit_oid'], scratch / 'roadmap', daemon.workspace_roots,
                 daemon.publication_remotes, daemon._publication_headers)
        renew()
        ensure_commit(job['workspace'], request['base_commit_oid'], scratch / 'roadmap',
                      daemon.publication_remotes, daemon._publication_headers)
        if job['solution_workspace']:
            renew()
            checkout(job['solution_workspace'], request['implementation_commit_oid'], scratch / 'solution',
                     daemon.workspace_roots, daemon.publication_remotes, daemon._publication_headers)
            config = (scratch / 'solution' / request['comparator_config']).resolve(strict=True)
            if not config.is_relative_to(scratch / 'solution'):
                raise ValueError('Comparator config escapes the pinned solution')
            inputs.update(solution_root=str(scratch / 'solution'), comparator_config=str(config))
        renew()
        (scratch / 'input.json').write_text(json.dumps(inputs, default=str))
        with (scratch / 'diagnostic.log').open('wb') as log:
            process = subprocess.Popen([sys.executable, '-m', __name__, str(scratch)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                env={**os.environ, **getattr(harness, 'environment', {})})
            deadline = time.monotonic() + 8 * policy.timeout_seconds + 120
            try:
                while process.poll() is None:
                    daemon._progress('milestone_verification', deadline_seconds=120)
                    if stop.wait(1):
                        raise InterruptedError('Build host stopping; verification lease will be reclaimed')
                    if time.monotonic() > deadline:
                        raise TimeoutError('Milestone verification exceeded its total deadline')
                    if time.monotonic() - heartbeat_at >= 20:
                        renew()
                if process.returncode:
                    with (scratch / 'diagnostic.log').open('rb') as diagnostic:
                        diagnostic.seek(max(0, diagnostic.seek(0, 2) - 5000))
                        raise ValueError(diagnostic.read().decode(errors='replace'))
                return json.loads((scratch / 'report.json').read_text())
            finally:
                stop_process(process)


def serve(daemon, stop):
    while not stop.is_set():
        job = None
        try:
            daemon._progress('milestone_check_claim', deadline_seconds=150)
            response = daemon.transport._post('/api/v3/worker/milestone-jobs/claim', {'host_id': daemon.host_id})
            response.raise_for_status()
            job = response.json()['job']
            if job:
                try:
                    outcome = {'report': execute(daemon, job, stop)}
                except InterruptedError:
                    return
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    outcome = {'error': str(error)[-6000:] or type(error).__name__}
                response = daemon.transport._post('/api/v3/worker/milestone-jobs/' + job['id'] + '/finish',
                    {'claim_token': job['claim_token'], **outcome}, key=job['id'] + ':' + job['claim_token'])
                response.raise_for_status()
        except (httpx.HTTPError, OSError, ValueError, KeyError):
            logging.exception('Milestone checker request failed; its lease remains recoverable')
        daemon._progress('idle', deadline_seconds=150)
        if stop.wait(1 if job else 10):
            return


def main():
    scratch = Path(sys.argv[1])
    inputs = json.loads((scratch / 'input.json').read_text())
    policy = inputs['policy']
    policy['root'] = Path(policy['root'])
    report = verify(Path(inputs['root']), inputs['base'], LeanBuildPolicy(**policy),
        solution_root=Path(inputs['solution_root']) if inputs.get('solution_root') else None,
        comparator_config=Path(inputs['comparator_config']) if inputs.get('comparator_config') else None)
    (scratch / 'report.json').write_text(json.dumps(report))


if __name__ == '__main__':
    main()
