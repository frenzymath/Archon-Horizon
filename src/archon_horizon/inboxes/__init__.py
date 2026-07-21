"""Inbox provider abstractions and implementations."""

from .base import InboxProvider
from .filesystem import FilesystemInboxProvider
from .github import GithubInboxProvider

__all__ = [
    "FilesystemInboxProvider",
    "GithubInboxProvider",
    "InboxProvider",
]

