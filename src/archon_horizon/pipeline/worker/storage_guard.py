"""Host-level storage floors and cleanup targets for worker health reporting."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import shutil
from typing import Iterable


@dataclass(frozen=True)
class StorageRoot:
    """One configured path and the filesystem that contains it."""

    name: str
    path: str
    filesystem: str
    total_bytes: int
    free_bytes: int
    required_free_bytes: int
    cleanup_target_percent: int
    cleanup_target_bytes: int

    @property
    def status(self) -> str:
        return "ready" if self.free_bytes >= self.required_free_bytes else "storage_pressure"

    def as_dict(self) -> dict[str, int | str]:
        return {
            "name": self.name,
            "path": self.path,
            "filesystem": self.filesystem,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "required_free_bytes": self.required_free_bytes,
            "cleanup_target_percent": self.cleanup_target_percent,
            "cleanup_target_bytes": self.cleanup_target_bytes,
            "cleanup_recommended": self.free_bytes < self.cleanup_target_bytes,
            "status": self.status,
        }


def _existing_ancestor(path: Path) -> Path:
    candidate = path.absolute()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.resolve()


def inspect_storage(
    roots: Iterable[tuple[str, Path]],
    *,
    cleanup_target_percent: int = 20,
    minimum_free_bytes: int = 0,
) -> dict:
    """Inspect all managed roots without creating or deleting anything.

    The percentage is reported as a cleanup target. Paths on the same
    filesystem are still reported separately so an operator can see which
    configured root is responsible for pressure. Admission uses only the
    explicit byte floor.
    """
    if type(cleanup_target_percent) is not int or not 0 <= cleanup_target_percent <= 90:
        raise ValueError("storage cleanup target percent must be an integer from 0 through 90")
    if type(minimum_free_bytes) is not int or minimum_free_bytes < 0:
        raise ValueError("minimum free bytes must be a nonnegative integer")

    observed: list[StorageRoot] = []
    for name, configured in roots:
        if not isinstance(configured, Path) or not configured.is_absolute():
            raise ValueError("managed storage roots must be absolute paths")
        filesystem = _existing_ancestor(configured)
        usage = shutil.disk_usage(filesystem)
        required = minimum_free_bytes
        target = math.ceil(usage.total * cleanup_target_percent / 100)
        observed.append(StorageRoot(name, str(configured), str(filesystem), usage.total,
                                    usage.free, required, cleanup_target_percent, target))

    pressured = [item for item in observed if item.status == "storage_pressure"]
    return {
        "status": "storage_pressure" if pressured else "ready",
        "cleanup_target_percent": cleanup_target_percent,
        "cleanup_target_bytes": max((item.cleanup_target_bytes for item in observed), default=0),
        "cleanup_recommended": any(item.free_bytes < item.cleanup_target_bytes for item in observed),
        "roots": [item.as_dict() for item in observed],
        "free_bytes": min((item.free_bytes for item in observed), default=0),
        "required_free_bytes": max((item.required_free_bytes for item in observed), default=0),
    }
