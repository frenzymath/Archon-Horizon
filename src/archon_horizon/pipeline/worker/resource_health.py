"""Cheap Linux resource observations for admission, without killing active work.

Thresholds are conservative tuning defaults, not estimates of a Lean build's
peak memory. Operators should measure their hosts and configure build limits.
Unavailable observations stay unknown; they are never reported as free capacity.
"""
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ResourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    minimum_available_memory_bytes: int = Field(default=1024**3, ge=0)
    maximum_memory_psi_avg10: float = Field(default=20, ge=0, le=100)
    maximum_io_psi_avg10: float = Field(default=50, ge=0, le=100)
    maximum_cpu_psi_avg10: float = Field(default=95, ge=0, le=100)


def _read(path):
    try:
        return path.read_text()
    except (OSError, UnicodeError):
        return None


def _psi(path):
    value = _read(path)
    try:
        return float(dict(field.split("=") for field in value.splitlines()[0].split()[1:])["avg10"]) if value else None
    except (ValueError, KeyError, IndexError):
        return None


def observe(policy, *, proc=Path("/proc"), cgroup=Path("/sys/fs/cgroup")):
    """Use the tightest readable host/cgroup limit, including ancestor limits."""
    available = None
    try:
        memory = dict(line.split(":", 1) for line in (_read(proc / "meminfo") or "").splitlines())
        available = int(memory["MemAvailable"].split()[0]) * 1024
    except (KeyError, ValueError, IndexError):
        pass
    membership = _read(proc / "self/cgroup") or ""
    relative = next((line[3:] for line in membership.splitlines() if line.startswith("0::")), "/")
    root = cgroup.resolve()
    current = (root / relative.lstrip("/")).resolve()
    cpu_limit = None
    if current.is_relative_to(root):
        for directory in (current, *current.parents):
            if not directory.is_relative_to(root):
                break
            try:
                limit = _read(directory / "memory.max")
                used = _read(directory / "memory.current")
                if limit and used and limit.strip() != "max":
                    remaining = max(0, int(limit) - int(used))
                    available = remaining if available is None else min(available, remaining)
                cpu = (_read(directory / "cpu.max") or "").split()
                if len(cpu) == 2 and cpu[0] != "max":
                    cores = int(cpu[0]) / int(cpu[1])
                    cpu_limit = cores if cpu_limit is None else min(cpu_limit, cores)
            except (ValueError, ZeroDivisionError):
                pass
    pressure = {}
    reasons = []
    for resource in ("memory", "io", "cpu"):
        observations = [_psi(proc / "pressure" / resource)]
        if current.is_relative_to(root):
            observations += [_psi(current / (resource + ".pressure"))]
        known = [value for value in observations if value is not None]
        pressure[resource] = max(known) if known else None
        threshold = getattr(policy, f"maximum_{resource}_psi_avg10")
        if policy.enabled and known and max(known) >= threshold:
            reasons.append(f"{resource} pressure avg10 {max(known):.1f}% >= {threshold:.1f}%")
    if policy.enabled and available is not None and available < policy.minimum_available_memory_bytes:
        reasons.append(f"Available memory {available} below admission floor {policy.minimum_available_memory_bytes}")
    return {"status": "resource_pressure" if reasons else "ready", "available_memory_bytes": available,
            "cpu_quota_cores": cpu_limit, "psi_avg10": pressure, "reasons": reasons}
