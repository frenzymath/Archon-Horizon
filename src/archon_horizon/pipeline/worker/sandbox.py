from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .contracts import validate_tool_environment


@dataclass(frozen=True)
class SandboxMount:
    source: Path
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class SandboxPolicy:
    """Pin the image and resource policy used by a rootless provider container."""

    image_digest: str
    network: str = "outbound"
    memory_limit_bytes: int | None = None
    cpu_limit: int | None = None
    process_limit: int = 256
    extra_mounts: tuple[SandboxMount, ...] = ()

    def __post_init__(self) -> None:
        # A mutable image tag could silently change the enrolled execution
        # environment. Require a content digest to identify the selected image.
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", self.image_digest):
            raise ValueError("sandbox image must be pinned by SHA-256 digest")
        if self.network not in {"outbound", "none"}:
            raise ValueError("unsupported sandbox network policy")
        for value in (self.memory_limit_bytes, self.cpu_limit, self.process_limit):
            if value is not None and (isinstance(value, bool) or value <= 0):
                raise ValueError("sandbox resource limits must be positive")


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def podman_command(policy: SandboxPolicy, *, workspace: Path, provider_home: Path,
                   scratch: Path, protected_roots: tuple[Path, ...],
                   command: list[str], name: str, uid: int | None = None,
                   gid: int | None = None, environment_names: tuple[str, ...] = (),
                   skill_bundle: Path | None = None, podman_executable: str = "podman",
                   environment_values: dict[str, str] | None = None,
                   workspace_read_only: bool = False) -> list[str]:
    """Build a Podman argv without launching or weakening the requested isolation.

    Mounts expose only selected workspace, provider, scratch and policy paths.
    Protected worker/control state is excluded, with one read-only exception for
    the daemon's verified instruction bundle. ``outbound`` selects slirp4netns;
    it is not a destination allowlist.
    """
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    validate_tool_environment(environment_values or {})
    if uid == 0 or gid == 0:
        raise ValueError("rootless sandbox requires a non-root host identity")
    if not re.fullmatch(r"horizon-[a-zA-Z0-9_-]+", name) or not command:
        raise ValueError("invalid owned container name or empty command")
    mounts = [SandboxMount(workspace, "/workspace", workspace_read_only),
              SandboxMount(provider_home, "/provider-home", False),
              SandboxMount(scratch, "/tmp", False), SandboxMount(scratch, "/var/tmp", False),
              *policy.extra_mounts]
    protected = tuple(p.resolve() for p in protected_roots)
    if skill_bundle is not None:
        if (skill_bundle.is_symlink() or not re.fullmatch("[0-9a-f]{64}", skill_bundle.name)
                or skill_bundle.parent.name != "bundles" or skill_bundle.parent.parent.resolve() not in protected):
            raise ValueError("skill mount must be a verified daemon bundle")
        mounts.append(SandboxMount(skill_bundle, "/horizon-skills", True))
    targets: list[PurePosixPath] = []
    sources: list[Path] = []
    # Keep the image filesystem read-only and drop capabilities/privilege gains.
    # keep-id maps file ownership to the calling user. Both temporary directories
    # bind host scratch rather than using implicit container tmpfs storage.
    args = [podman_executable, "run", "--rm", "--interactive", "--name", name,
            "--label", "org.archon-horizon.managed=true", "--read-only",
            "--read-only-tmpfs=false", "--userns=keep-id", "--user", f"{uid}:{gid}",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit", str(policy.process_limit),
            "--network", "none" if policy.network == "none" else "slirp4netns",
            "--workdir", "/workspace", "--env", "HOME=/provider-home",
            "--env", "TMPDIR=/tmp", "--env", "TMP=/tmp", "--env", "TEMP=/tmp",
            "--env", "CODEX_HOME=/provider-home/.codex", "--env", "CLAUDE_CONFIG_DIR=/provider-home/.claude"]
    if policy.memory_limit_bytes is not None:
        args.extend(["--memory", str(policy.memory_limit_bytes)])
    if policy.cpu_limit is not None:
        args.extend(["--cpus", str(policy.cpu_limit)])
    for key in environment_names:
        # Execution credentials are supplied separately; a host/enrollment key
        # must not be forwarded into the provider's container environment.
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in {"HORIZON_HOST_TOKEN", "HORIZON_ENROLLMENT_TOKEN"}:
            raise ValueError("unsafe sandbox environment name")
        explicit = (environment_values or {}).get(key)
        args.extend(["--env", key if explicit is None else key + "=" + explicit])
    for mount in mounts:
        source = mount.source.resolve(strict=True)
        target = PurePosixPath(mount.target)
        owned_bundle = skill_bundle is not None and mount.source == skill_bundle and mount.target == "/horizon-skills" and mount.read_only
        if not source.is_dir() or source == Path("/") or (not owned_bundle and any(_overlaps(source, p) for p in protected)):
            raise ValueError("mount overlaps protected daemon/control state or is not a directory")
        if (not target.is_absolute() or ".." in target.parts or str(target) != mount.target
                or target == PurePosixPath("/") or target.parts[1] in {"proc", "sys", "dev", "run"}):
            raise ValueError("unsafe sandbox mount target")
        if any(target == previous or target in previous.parents or previous in target.parents for previous in targets):
            raise ValueError("overlapping sandbox mount targets")
        if "," in str(source) or "," in str(target) or "\n" in str(source):
            raise ValueError("unsupported mount path delimiter")
        if any(_overlaps(source, previous) for previous in sources) and not (source == scratch.resolve() and target == PurePosixPath("/var/tmp")):
            raise ValueError("sandbox mount sources overlap")
        targets.append(target)
        sources.append(source)
        args.extend(["--mount", f"type=bind,src={source},dst={target}," + ("ro" if mount.read_only else "rw")])
    return [*args, "--", policy.image_digest, *command]
