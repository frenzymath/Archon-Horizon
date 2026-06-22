"""Descriptor-backed subagent registry.

Discovery mirrors Archon's model, adapted to Horizon's workspace state:

* built-in deterministic subagents remain available by name;
* workspace descriptors live under ``.archon-horizon/subagents/<name>.md``;
* a descriptor names a harness, or falls back to the informal harness.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from archon_horizon.harnesses.base import Harness

from .base import DescriptorSubagent, Subagent, SubagentDescriptor

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*(?:\n|$)", re.DOTALL)


class SubagentRegistry:
    def __init__(self, subagents: Mapping[str, Subagent]) -> None:
        self._subagents = dict(subagents)

    def get(self, name: str) -> Subagent | None:
        return self._subagents.get(name)

    def names(self) -> list[str]:
        return sorted(self._subagents)

    def all(self) -> tuple[Subagent, ...]:
        return tuple(self._subagents[name] for name in self.names())


def parse_descriptor_file(path: Path) -> SubagentDescriptor:
    text = path.read_text("utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError(f"{path}: missing YAML frontmatter")
    raw = yaml.safe_load(match.group(1)) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: frontmatter must be a mapping")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{path}: missing non-empty name")
    if name != path.stem:
        raise ValueError(f"{path}: name {name!r} does not match filename stem {path.stem!r}")
    return SubagentDescriptor(
        name=name,
        description=str(raw.get("description") or ""),
        harness=str(raw["harness"]) if raw.get("harness") else None,
        write_domain=str(raw["write_domain"]) if raw.get("write_domain") else None,
        read_only=bool(raw.get("read_only", False)),
        default_enabled=bool(raw.get("default_enabled", True)),
        prompt_body=text[match.end():],
        source_path=path,
    )


def load_descriptors(directory: Path) -> dict[str, SubagentDescriptor]:
    descriptors: dict[str, SubagentDescriptor] = {}
    if not directory.is_dir():
        return descriptors
    for path in sorted(directory.glob("*.md")):
        descriptors[path.stem] = parse_descriptor_file(path)
    return descriptors


def build_registry(
    descriptor_dir: Path,
    *,
    harnesses: Mapping[str, Harness] = {},
    default_harness: Harness | None = None,
) -> SubagentRegistry:
    subagents: dict[str, Subagent] = {}
    builtin_dir = Path(__file__).parent / "descriptors"
    all_descriptors = load_descriptors(builtin_dir)
    all_descriptors.update(load_descriptors(descriptor_dir))
    
    for descriptor in all_descriptors.values():
        harness = harnesses.get(descriptor.harness or "") or default_harness
        if harness is None:
            continue
        subagents[descriptor.name] = DescriptorSubagent(descriptor, harness)
    return SubagentRegistry(subagents)


def _enabled_names(names: Sequence[str] | str | None, registry: SubagentRegistry) -> list[str]:
    if names is None:
        return registry.names()
    if isinstance(names, str):
        return registry.names() if names == "*" else [names]
    return list(names)


def build_subagents(
    names: Sequence[str] | str | None = None,
    *,
    descriptor_dir: Path | None = None,
    harnesses: Mapping[str, Harness] = {},
    default_harness: Harness | None = None,
) -> tuple[Subagent, ...]:
    """Instantiate subagents by name; ``None`` keeps all available defaults."""
    registry = build_registry(
        descriptor_dir or Path(),
        harnesses=harnesses,
        default_harness=default_harness,
    )
    return tuple(
        subagent
        for name in _enabled_names(names, registry)
        if (subagent := registry.get(name)) is not None
    )


def descriptor_summary(descriptor_dir: Path) -> str:
    builtin_dir = Path(__file__).parent / "descriptors"
    descriptors = load_descriptors(builtin_dir)
    descriptors.update(load_descriptors(descriptor_dir))
    if not descriptors:
        return "(no descriptor subagents installed)"
    lines: list[str] = []
    for descriptor in descriptors.values():
        harness = descriptor.harness or "informal-agent harness"
        domain = descriptor.write_domain or "(caller declares)"
        desc = descriptor.description.strip() or "no description"
        lines.append(f"- {descriptor.name} [{harness}] write: {domain} - {desc}")
    return "\n".join(lines)
