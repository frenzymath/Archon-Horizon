"""Descriptor-backed subagents the Horizon agent delegates to."""

from __future__ import annotations

from .base import DescriptorSubagent, Subagent, SubagentContext, SubagentDescriptor, SubagentResult
from .registry import (
    SubagentRegistry,
    build_registry,
    descriptor_summary,
    load_descriptors,
    parse_descriptor_file,
)

__all__ = [
    "DescriptorSubagent",
    "Subagent",
    "SubagentContext",
    "SubagentDescriptor",
    "SubagentRegistry",
    "SubagentResult",
    "build_registry",
    "descriptor_summary",
    "load_descriptors",
    "parse_descriptor_file",
]
