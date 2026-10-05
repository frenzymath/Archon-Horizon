"""Prepare isolated retained worktrees from verified host-local Git objects."""

import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import sys
import tempfile
from uuid import UUID

from .provider import process_identity, terminate_owned_process
from .journal import boot_identity


def prepare_workspace(grant, roots, state_root, keep_alive, record_process=lambda value: None):
    manifest = grant.workspace_preparation
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported workspace preparation manifest")
    if set(manifest) != {"schema_version", "source_workspace_id", "source_path", "commit_oid", "branch_name"}:
        raise ValueError("invalid workspace preparation fields")
    if not all(isinstance(manifest[key], str) for key in ("source_workspace_id", "source_path", "commit_oid", "branch_name")):
        raise ValueError("invalid workspace preparation field types")
    UUID(manifest["source_workspace_id"])
    assignment_id, workspace_id = str(UUID(grant.assignment_id)), str(UUID(grant.workspace_id))
    source = Path(manifest["source_path"])
    destination = Path(grant.workspace_path)
    roots = tuple(root.resolve(strict=True) for root in roots)
    for path in (source, destination):
        if not path.is_absolute() or path.resolve() != path or not any(path.is_relative_to(root) for root in roots):
            raise ValueError("workspace preparation path escaped enabled roots")
    if destination.parts[-2:] != ("assignments", assignment_id) or source == destination:
        raise ValueError("workspace preparation requires an assignment-specific destination")
    if destination.is_relative_to(state_root) or state_root.is_relative_to(destination):
        raise ValueError("prepared workspace overlaps worker state")
    if not source.is_dir():
        raise ValueError("workspace preparation source is unavailable")
    commit, branch = manifest["commit_oid"], manifest["branch_name"]
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("workspace preparation requires an exact commit")
    if branch != "horizon/assignments/" + assignment_id:
        raise ValueError("workspace preparation branch does not match the assignment")
    private = state_root / "workspace-preparations"
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    intent = private / (workspace_id + ".json")
    staged = destination.parent / (".horizon-preparing-" + workspace_id)
    expected = {"workspace_id": workspace_id, "path": str(destination), **manifest}
    with (private / (workspace_id + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def git(path, *args):
            keep_alive()
            command = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(path), *args]
            read_gate, write_gate = os.pipe()
            process = None
            identity = None
            deadline = time.monotonic() + 900
            try:
                bootstrap = "import os,sys; fd=int(sys.argv[1]); ok=os.read(fd,1); os.close(fd); " \
                            "sys.exit(125) if ok!=b'1' else os.execvp(sys.argv[2],sys.argv[2:])"
                process = subprocess.Popen([sys.executable, "-c", bootstrap, str(read_gate), *command],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                    pass_fds=(lock.fileno(), read_gate))
                os.close(read_gate)
                read_gate = -1
                identity = {"pid": process.pid, "process_identity": process_identity(process.pid), "boot_id": boot_identity()}
                record_process(identity)
                os.write(write_gate, b"1")
                os.close(write_gate)
                write_gate = -1
                while True:
                    try:
                        output, _ = process.communicate(timeout=.25)
                        break
                    except subprocess.TimeoutExpired:
                        keep_alive()
                        if time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired("workspace preparation", 900)
                if process.returncode:
                    raise RuntimeError("Git workspace preparation failed; retained intent permits inspection and retry")
                keep_alive()
                return output.decode().strip()
            finally:
                for descriptor in (read_gate, write_gate):
                    if descriptor >= 0:
                        os.close(descriptor)
                if process is not None:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    if identity is not None:
                        terminate_owned_process(identity)
                    record_process({"pid": None, "process_identity": None})

        common = Path(git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if not any(common.is_relative_to(root) for root in roots):
            raise ValueError("source Git metadata escaped enabled roots")
        if git(source, "rev-parse", "--verify", commit + "^{commit}") != commit:
            raise ValueError("workspace source does not contain the pinned commit")
        if intent.exists():
            if json.loads(intent.read_text()) != expected:
                raise ValueError("workspace preparation intent changed")
        else:
            if destination.exists():
                raise ValueError("refusing to adopt an unrecorded workspace directory")
            descriptor, temporary = tempfile.mkstemp(dir=private, prefix=".intent-")
            try:
                with os.fdopen(descriptor, "w") as output:
                    json.dump(expected, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, intent)
            finally:
                Path(temporary).unlink(missing_ok=True)
            directory_fd = os.open(private, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.parent.resolve() != destination.parent:
                raise ValueError("workspace preparation parent was redirected")
            if staged.is_symlink() or staged.resolve() != staged:
                raise ValueError("workspace preparation staging directory was redirected")
            if not staged.exists():
                existing = git(source, "for-each-ref", "--format=%(objectname)", "refs/heads/" + branch)
                if existing and existing != commit:
                    raise ValueError("workspace preparation branch contains unexpected work")
                options = [branch] if existing else ["-b", branch, commit]
                git(source, "worktree", "add", "--quiet", "--no-checkout", "--lock", str(staged), *options)
            if (Path(git(staged, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve() != common
                    or git(staged, "symbolic-ref", "--short", "HEAD") != branch
                    or git(staged, "rev-parse", "HEAD") != commit):
                raise ValueError("workspace staging directory differs from its pinned identity")
            # Only this private, never-admitted staging directory is reset. Once
            # renamed to the grant path, all changes are preserved on every retry.
            git(staged, "read-tree", "--reset", "-u", commit)
            keep_alive()
            os.rename(staged, destination)
            git(source, "worktree", "repair", str(destination))
        else:
            # A crash after rename can leave Git's reverse path at the old stage.
            git(source, "worktree", "repair", str(destination))
        if Path(git(destination, "rev-parse", "--show-toplevel")).resolve() != destination:
            raise ValueError("prepared directory is not a separate worktree")
        if Path(git(destination, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve() != common:
            raise ValueError("prepared workspace Git objects do not match the source")
        if git(destination, "rev-parse", "HEAD") != commit or git(destination, "symbolic-ref", "--short", "HEAD") != branch:
            raise ValueError("prepared workspace differs from its pinned identity")
        if git(destination, "status", "--porcelain", "--untracked-files=normal"):
            raise ValueError("prepared workspace contains changes; preserving it for recovery")
    return {"workspace_id": workspace_id, "head_commit_oid": commit, "branch_name": branch}
