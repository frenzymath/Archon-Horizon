from __future__ import annotations

import os
import hashlib
import subprocess
import uuid
import fcntl
import errno
import shutil
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import Operation, require_identifier
from .journal import DurableJournal


class PublicationBlocked(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GitRecovery:
    """Preserve owned Git history independently of agent/API acknowledgement."""

    snapshot_timeout_seconds = 300

    def __init__(self, workspace: Path, repository_id: str, execution_id: str,
                 epoch: int, journal: DurableJournal, *, publication_header: str | None = None) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.repository_id = require_identifier(repository_id)
        self.execution_id = require_identifier(execution_id)
        self.epoch = epoch
        self.journal = journal
        if publication_header is not None and ("\n" in publication_header or "\r" in publication_header):
            raise ValueError("publication header must contain one HTTP header")
        self._publication_header = publication_header
        self.prefix = f"refs/horizon/recovery/{self.execution_id}/"
        self.remote_prefix = f"refs/heads/horizon/recovery/{self.execution_id}/"
        if self._git("rev-parse", "--show-toplevel").strip() != str(self.workspace):
            raise ValueError("recovery requires the root of an exclusive Git checkout")
        common = Path(self._git("rev-parse", "--git-common-dir").strip())
        if not common.is_absolute():
            common = self.workspace / common
        self.objects = (common / "objects").resolve(strict=True)
        self.trusted_git = self.journal.state_root / "git-recovery" / self.execution_id
        self.trusted_git.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._snapshot_lock():
            if not self.trusted_git.exists():
                self._git("init", "--bare", "--template=", str(self.trusted_git))

    @contextmanager
    def _snapshot_lock(self):
        # Only local snapshot operations share mutable trusted HEAD/index state.
        # Remote I/O never holds this lock.
        with self.trusted_git.with_suffix(".lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            # A publisher must never wait indefinitely behind a large snapshot.
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def _environment(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        return {"PATH": os.defpath, "HOME": str(self.journal.state_root / "git-home"),
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                "GIT_SSH_COMMAND": "ssh -F /dev/null -o BatchMode=yes", **(overrides or {})}

    def _git(self, *args: str, env: dict[str, str] | None = None,
             input_text: str | None = None) -> str:
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                                 "-c", "log.showSignature=false", *args],
                                cwd=self.workspace, input=input_text, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=self.snapshot_timeout_seconds if any(command in args for command in
                                    ("add", "status", "write-tree", "read-tree", "rev-list")) else 30,
                                check=False, env=self._environment(env))
        if result.returncode:
            message = result.stderr.lower()
            if "no space left on device" in message or "disk quota exceeded" in message:
                raise OSError(errno.ENOSPC, "Git recovery storage exhausted")
            if any(command in args for command in ("ls-remote", "push")):
                if any(fragment in message for fragment in ("authentication failed", "permission denied (publickey)",
                       "could not read username", "requested url returned error: 401", "requested url returned error: 403")):
                    raise PublicationBlocked("git_authentication_failed")
                if any(fragment in message for fragment in ("does not appear to be a git repository", "repository not found",
                       "pre-receive hook declined", "deny updating", "protected branch")):
                    raise PublicationBlocked("git_remote_configuration")
            raise RuntimeError(f"git {args[0]} failed ({result.returncode}): {result.stderr[-2000:]}")
        return result.stdout

    def _trusted(self, *args: str, env: dict[str, str] | None = None,
                 input_text: str | None = None) -> str:
        overrides = {"GIT_OBJECT_DIRECTORY": str(self.objects), **(env or {})}
        return self._git(f"--git-dir={self.trusted_git}", f"--work-tree={self.workspace}",
                         "-c", "credential.helper=", "-c", "protocol.ext.allow=never", *args,
                         env=overrides, input_text=input_text)

    def _record_commit(self, oid: str) -> bool:
        resolved = self._git("rev-parse", "--verify", f"{oid}^{{commit}}").strip()
        recovery_ref = self.prefix + resolved
        self._git("update-ref", recovery_ref, resolved)
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"horizon:{self.repository_id}:{self.execution_id}:{resolved}"))
        local_key = str(uuid.uuid5(uuid.NAMESPACE_URL, key + ":preserve"))
        if self.journal.operation(key) is not None and self.journal.operation(local_key) is not None:
            return False
        # Commit identity supplies a stable operation key and timestamp across scans.
        timestamp = max(1, int(self._git("show", "-s", "--format=%ct", resolved).strip()))
        operation = Operation.create(self.execution_id, self.epoch, "publication_discovered",
                                     {"repository_id": self.repository_id, "commit_oid": resolved,
                                      "recovery_ref": recovery_ref, "workspace_path": str(self.workspace)},
                                     operation_id=key, occurred_at=timestamp)
        created = self.journal.enqueue(operation)
        local_job = Operation.create(self.execution_id, self.epoch, "publication_discovered", operation.payload,
                                     operation_id=local_key,
                                     occurred_at=timestamp)
        self.journal.enqueue(local_job, destination="local_git")
        return created

    def reconcile(self, *, owned_refs: tuple[str, ...] = ("HEAD",), limit: int = 256) -> list[str]:
        if limit < 1:
            raise ValueError("reconciliation limit must be positive")
        tips: set[str] = set()
        for ref in owned_refs:
            if ref != "HEAD" and not ref.startswith("refs/heads/"):
                raise ValueError("only HEAD and explicitly owned branches can be scanned")
            tips.add(self._git("rev-parse", "--verify", f"{ref}^{{commit}}").strip())
            tips.update(self._git("reflog", "show", "--format=%H", ref).splitlines())
        tips.update(self._git("for-each-ref", "--format=%(objectname)", self.prefix).splitlines())
        if not tips:
            return []
        # Remote tracking refs bound the historical scan. Every locally owned tip
        # still gets a preservation record, even when it appears in a remote ref.
        ancestors = self._git("rev-list", f"--max-count={limit}", *sorted(tips), "--not", "--remotes").splitlines()
        discovered = []
        # Preserved tips keep their entire reachable history safe. Bound per-commit
        # indexing so a large imported repository cannot block recovery of new work.
        for oid in [*sorted(tips), *sorted(set(ancestors) - tips)]:
            if self._record_commit(oid):
                discovered.append(oid)
        return discovered

    def record_head(self) -> None:
        self._record_commit(self._git("rev-parse", "--verify", "HEAD^{commit}").strip())

    def checkpoint_dirty(self) -> str | None:
        with self._snapshot_lock():
            return self._checkpoint_dirty()

    def _checkpoint_dirty(self) -> str | None:
        if shutil.disk_usage(self.objects).free < self.journal.minimum_free_bytes + 65536:
            from .contracts import JournalFull
            raise JournalFull("Git checkpoint disk reserve reached; retain unpublished work")
        index = self.journal.state_root / f"recovery-index-{uuid.uuid4().hex}"
        stage_index = index.with_suffix(".staged")
        env = {"GIT_INDEX_FILE": str(index), "GIT_AUTHOR_NAME": "Horizon recovery",
               "GIT_AUTHOR_EMAIL": "recovery@horizon.invalid", "GIT_COMMITTER_NAME": "Horizon recovery",
               "GIT_COMMITTER_EMAIL": "recovery@horizon.invalid"}
        try:
            index_path = Path(self._git("rev-parse", "--git-path", "index").strip())
            if not index_path.is_absolute():
                index_path = self.workspace / index_path
            git_dir = Path(self._git("rev-parse", "--absolute-git-dir").strip())
            if any((git_dir / name).exists() for name in ("index.lock", "HEAD.lock", "packed-refs.lock", "shallow.lock")):
                raise BlockingIOError("workspace Git operation in progress")
            if index_path.is_file():
                data = index_path.read_bytes()
                with stage_index.open("xb") as output:
                    os.chmod(stage_index, 0o600)
                    output.write(data)
                saved_indexes = self.journal.state_root / "git-indexes" / self.execution_id
                saved_indexes.mkdir(parents=True, mode=0o700, exist_ok=True)
                saved_index = saved_indexes / hashlib.sha256(data).hexdigest()
                if not saved_index.exists() or saved_index.read_bytes() != data:
                    temporary = saved_indexes / (saved_index.name + ".pending-" + uuid.uuid4().hex)
                    with temporary.open("xb") as output:
                        os.chmod(temporary, 0o600)
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, saved_index)
                    directory_fd = os.open(saved_indexes, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            parent = self._git("rev-parse", "--verify", "HEAD").strip()
            parent_tree = self._git("rev-parse", parent + "^{tree}").strip()
            self._trusted("update-ref", "HEAD", parent)
            if not stage_index.exists():
                self._trusted("read-tree", parent, env={"GIT_INDEX_FILE": str(stage_index)})
            if not self._trusted("status", "--porcelain=v1", "--untracked-files=all",
                                 env={"GIT_INDEX_FILE": str(stage_index)}).strip():
                return None
            unresolved = self._trusted("ls-files", "--unmerged", env={"GIT_INDEX_FILE": str(stage_index)})
            stage_tree = None if unresolved else self._trusted("write-tree", env={"GIT_INDEX_FILE": str(stage_index)}).strip()
            if stage_tree is not None and stage_tree != parent_tree:
                stage_ref = f"refs/horizon/staged/{self.execution_id}"
                previous_stage = self._git("for-each-ref", "--format=%(objectname)", stage_ref).strip()
                if previous_stage and stage_tree == self._git("rev-parse", previous_stage + "^{tree}").strip():
                    staged = previous_stage
                else:
                    staged = self._trusted("commit-tree", stage_tree, "-p", parent, env=env,
                                           input_text=f"Horizon staged recovery for {self.execution_id}\n").strip()
                    self._git("update-ref", stage_ref, staged)
                self._record_commit(staged)
            self._trusted("read-tree", parent, env=env)
            self._trusted("add", "-A", "--", ".", env=env)
            tree = self._trusted("write-tree", env=env).strip()
            if tree == parent_tree:
                return None
            dirty_ref = f"refs/horizon/dirty/{self.execution_id}"
            previous = self._git("for-each-ref", "--format=%(objectname)", dirty_ref).strip()
            if previous and tree == self._git("rev-parse", previous + "^{tree}").strip():
                self._record_commit(previous)
                return previous
            oid = self._trusted("commit-tree", tree, "-p", parent, env=env,
                            input_text=f"Horizon recovery checkpoint for {self.execution_id}\n").strip()
            self._git("update-ref", dirty_ref, oid)
            self._record_commit(oid)
            return oid
        finally:
            index.unlink(missing_ok=True)
            index.with_name(index.name + ".lock").unlink(missing_ok=True)
            stage_index.unlink(missing_ok=True)
            stage_index.with_name(stage_index.name + ".lock").unlink(missing_ok=True)

    def preserve_remote(self, oid: str, *, remote: str) -> str:
        """Publish only an immutable recovery ref, never a project branch/merge."""
        if not remote or remote.startswith("-"):
            raise ValueError("invalid configured Git remote")
        try:
            oid = self._git("rev-parse", "--verify", f"{oid}^{{commit}}").strip()
        except RuntimeError as error:
            raise PublicationBlocked("local_object_missing") from error
        ref = self.remote_prefix + oid
        self._git("update-ref", ref, oid)
        transport_env: dict[str, str] = {}
        if self._publication_header:
            destination = urlsplit(remote)
            loopback = destination.scheme == "http" and destination.hostname in {"127.0.0.1", "::1", "localhost"}
            if (not destination.hostname or destination.username is not None or destination.password is not None
                    or destination.fragment or destination.query or any(ord(char) < 33 for char in remote)
                    or not (destination.scheme == "https" or loopback)):
                raise ValueError("publication authorization headers require HTTPS or an exact loopback HTTP destination without URL credentials")
            transport_env = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": f"http.{remote}.extraHeader",
                             "GIT_CONFIG_VALUE_0": self._publication_header,
                             "GIT_CONFIG_KEY_1": "http.followRedirects", "GIT_CONFIG_VALUE_1": "false"}
        observed = self._trusted("ls-remote", "--refs", remote, ref, env=transport_env).strip()
        if observed:
            if observed.split()[0] != oid:
                raise PublicationBlocked("git_recovery_ref_conflict")
            return ref
        self._trusted("push", f"--force-with-lease={ref}:", remote, f"{oid}:{ref}", env=transport_env)
        observed = self._trusted("ls-remote", "--refs", remote, ref, env=transport_env).strip()
        if not observed or observed.split()[0] != oid:
            raise RuntimeError("remote preservation was not confirmed")
        return ref
