"""Markdown documents with structured, extensible YAML front matter."""

from __future__ import annotations

import datetime
import json
from typing import Any


def _json_value(value: Any, seen: set[int] | None = None) -> Any:
    seen = set() if seen is None else seen
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if id(value) in seen:
        raise ValueError("document metadata cannot contain recursive YAML aliases")
    seen.add(id(value))
    try:
        if isinstance(value, dict) and all(isinstance(key, str) for key in value):
            return {key: _json_value(item, seen) for key, item in value.items()}
        if isinstance(value, list):
            return [_json_value(item, seen) for item in value]
    finally:
        seen.remove(id(value))
    raise ValueError("document metadata must contain string keys and JSON-compatible values")


def parse_document(document: str) -> tuple[dict[str, Any], str]:
    if not isinstance(document, str):
        raise ValueError("document must be a string")
    lines = document.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, document
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() in {"---", "..."}), None)
    if closing is None:
        raise ValueError("document YAML front matter requires a closing --- line")
    raw = "".join(lines[1:closing])
    try:
        import yaml
    except ImportError:
        try:
            metadata = json.loads(raw) if raw.strip() else {}
        except ValueError as exc:
            raise ValueError("YAML documents require PyYAML (included with the API and worker extras)") from exc
    else:
        try:
            # The C implementation materially reduces graph-read latency when
            # thousands of document headers are parsed.  Both loaders retain
            # SafeLoader's constructor restrictions.
            loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
            metadata = yaml.load(raw, Loader=loader) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid document YAML: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("document YAML front matter must be an object")
    body = "".join(lines[closing + 1:])
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    return _json_value(metadata), body
