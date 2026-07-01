"""Helpers for sharded inbox provider files.

Inbox providers store item bodies as YAML and comments as Markdown files with
YAML frontmatter. Provider implementations hydrate comments back into
``InboxItem.metadata["comments"]`` for the rest of the application.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from archon_horizon.store.codec import YamlCodec

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_COMMENT_ID_RE = re.compile(r"C-(\d+)|ghc-(\d+)")


def without_comments(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return metadata suitable for storing on the item file itself.

    Comments and history are sharded into their own files and hydrated back onto
    ``metadata`` for reading; they must never be written into the item file.
    """

    clean = dict(metadata)
    clean.pop("comments", None)
    clean.pop("history", None)
    return clean


# ── history (append-only, one jsonl per item) ───────────────────────────
#
# History records the durable state transitions on an item (status, label,
# kind, body edits) so the UI can interleave them with comments on one
# chronological timeline. It is append-only — entries are never rewritten — so
# a plain jsonl is the simplest faithful store.


def append_history(path: Path, entry: dict[str, Any]) -> None:
    """Append one ``{at, actor, field, from, to, note}`` transition to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def next_comment_id(directory: Path, *, prefix: str = "C") -> str:
    highest = 0
    if directory.exists():
        for child in directory.glob("*.md"):
            match = _COMMENT_ID_RE.fullmatch(child.stem)
            if match:
                highest = max(highest, int(match.group(1) or match.group(2)))
    return f"{prefix}-{highest + 1:04d}"


def comment_sort_key(path: Path) -> tuple[int, str]:
    match = _COMMENT_ID_RE.fullmatch(path.stem)
    if match:
        return int(match.group(1) or match.group(2)), path.stem
    return 10**9, path.stem


def read_comment(path: Path) -> dict[str, Any]:
    text = path.read_text("utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {"id": path.stem, "body": text}
    meta_text, body = match.groups()
    metadata = YamlCodec().loads(meta_text) or {}
    comment = dict(metadata)
    comment.setdefault("id", path.stem)
    comment["body"] = body.lstrip("\n").rstrip("\n")
    return comment


def read_comments(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    return [read_comment(path) for path in sorted(directory.glob("*.md"), key=comment_sort_key)]


def write_comment(path: Path, comment: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(comment)
    body = str(payload.pop("body", ""))
    frontmatter = YamlCodec().dumps(payload).strip()
    path.write_text(f"---\n{frontmatter}\n---\n\n{body.rstrip()}\n", "utf-8")
