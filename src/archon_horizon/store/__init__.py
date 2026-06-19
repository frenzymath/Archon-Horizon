"""Persistence layer: abstract stores, codecs, and a filesystem backend."""

from __future__ import annotations

from .base import EventLog, MemoryStore, ProposalStore, RoadmapStore, TaskStore
from .codec import Codec, JsonCodec, YamlCodec
from .filesystem import (
    FilesystemEventLog,
    FilesystemMemoryStore,
    FilesystemProposalStore,
    FilesystemRoadmapStore,
    FilesystemTaskStore,
)

__all__ = [
    "Codec",
    "EventLog",
    "FilesystemEventLog",
    "FilesystemMemoryStore",
    "FilesystemProposalStore",
    "FilesystemRoadmapStore",
    "FilesystemTaskStore",
    "JsonCodec",
    "MemoryStore",
    "ProposalStore",
    "RoadmapStore",
    "TaskStore",
    "YamlCodec",
]
