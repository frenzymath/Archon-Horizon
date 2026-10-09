"""Remove retired generated worktrees only after checking their actual Git data."""
import os
import json
from pathlib import Path
import shutil
import subprocess
from uuid import UUID


def cleanup(workspace, roots, remote, header=None):
    """The API proves ownership is settled; this host proves source is published.

    A remote error, dirty tree, extra local commit or unexpected path preserves
    the directory. Ignore generated files according to Git, never source edits.
    """
    path = Path(workspace['path'])
    if path.is_symlink() or path.resolve() != path or not any(
            path.parent == root.resolve() / 'assignments' for root in roots):
        raise ValueError('Cleanup requires an ordinary generated assignment path')
    UUID(path.name)
    if not path.exists():
        return
    if not remote:
        raise ValueError('No configured durable publication remote; preserve workspace')
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    if header:
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.extraHeader', GIT_CONFIG_VALUE_0=header)
    def git(*args):
        return subprocess.run(['git', '-C', str(path), *args], env=env, capture_output=True,
                              check=True, timeout=120).stdout.decode().strip()
    if git('status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Workspace has unpublished file changes')
    # Ignoring a directory does not make its contents disposable. In
    # particular, .lake/packages can contain an edited dependency checkout.
    ignored = git('status', '--porcelain', '--ignored=matching', '--untracked-files=normal', '-z')
    for entry in ignored.split('\0'):
        if not entry.startswith('!! '):
            continue
        relative = entry[3:].rstrip('/')
        if relative not in ('.lake', '.horizon', '.horizon/reference-cache'):
            raise ValueError('Workspace has ignored data outside managed cache directories')
    local_state = path / '.horizon'
    if local_state.exists() and (local_state.is_symlink() or any(
            entry.name != 'reference-cache' for entry in local_state.iterdir())):
        raise ValueError('Workspace has retained local notes or state')
    lake = path / '.lake'
    if lake.exists():
        disposable = {'build', 'packages', 'lakefile.olean', 'lakefile.ilean', 'lakefile.olean.trace',
                      'lakefile.olean.lock', 'lakefile.olean.server', 'lakefile.olean.private'}
        if lake.is_symlink() or any(entry.name not in disposable for entry in lake.iterdir()):
            raise ValueError('Workspace has unknown Lake state; preserve it')
        packages = lake / 'packages'
        if packages.is_dir() and not packages.is_symlink():
            try:
                manifest = json.loads(git('show', 'HEAD:lake-manifest.json'))
                pinned = {entry['name']: entry['rev'] for entry in manifest.get('packages', [])
                          if entry.get('type') == 'git'}
            except (KeyError, TypeError, AttributeError) as error:
                raise ValueError('Workspace dependency manifest is malformed; preserve it') from error
            for dependency in packages.iterdir():
                if dependency.is_symlink():
                    continue  # Removing a link cannot remove the external source.
                revision = pinned.get(dependency.name)
                if not revision or not (dependency / '.git').is_dir():
                    raise ValueError('Workspace contains an unrecognized dependency checkout')
                if git('-C', str(dependency), 'status', '--porcelain', '--untracked-files=all') or git(
                        '-C', str(dependency), 'rev-list', '--all', '--not', revision):
                    raise ValueError('Workspace contains unpublished dependency changes')
    # Fetch only the durable visible branch; local donor refs do not establish
    # publication. Every local ref must be reachable, including recovery refs.
    branch = workspace['default_branch']
    git('check-ref-format', 'refs/heads/' + branch)
    git('fetch', '--quiet', '--no-tags', '--', remote, 'refs/heads/' + branch)
    if git('rev-list', '--all', '--not', 'FETCH_HEAD'):
        raise ValueError('Workspace contains commits absent from the durable visible branch')
    if (path / '.git').is_file():
        common = Path(git('rev-parse', '--git-common-dir'))
        if not common.is_absolute():
            common = (path / common).resolve()
        subprocess.run(['git', '--git-dir', str(common), 'worktree', 'remove', str(path)],
                       env=env, check=True, capture_output=True, timeout=120)
    elif (path / '.git').is_dir() and not (path / '.git').is_symlink():
        shutil.rmtree(path)
    else:
        raise ValueError('Unexpected Git metadata; preserve workspace')
