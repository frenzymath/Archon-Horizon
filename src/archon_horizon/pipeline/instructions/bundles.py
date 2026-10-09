"""Bounded, deterministic packaging of operator-selected skill files."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import yaml

from .._resources import PIPELINE_ROOT
from ..errors import DomainError
from .subagent_guidance import descriptor, overview as subagent_overview


def _skill_metadata(path: str, content: bytes) -> dict:
    try:
        text = content.decode("utf-8").replace("\r\n", "\n")
        frontmatter = {}
        if text.startswith("---\n"):
            header, separator, _ = text[4:].partition("\n---\n")
            if not separator:
                raise ValueError("unterminated skill frontmatter")
            frontmatter = yaml.safe_load(header) or {}
        if not isinstance(frontmatter, dict):
            raise ValueError("skill frontmatter must be a mapping")
        name = frontmatter.get("name", Path(path).parent.name)
        description = frontmatter.get("description", "Operator-provided skill")
        metadata = frontmatter.get("metadata", {})
        category = metadata.get("category", "custom") if isinstance(metadata, dict) else "custom"
        if not isinstance(name, str) or not isinstance(description, str) or not name.strip() or not description.strip():
            raise ValueError("skill name and description must be text")
        if category not in ("operations", "lean", "review", "custom"):
            category = "custom"
        return {"path": path, "name": " ".join(name.split())[:128],
                "description": " ".join(description.split())[:600], "category": category}
    except (UnicodeDecodeError, yaml.YAMLError, ValueError) as error:
        raise DomainError("invalid_skill_metadata", f"Invalid discovery metadata in {path}", 422) from error


def skill_overview(index: list[dict]) -> str:
    lines = ["# Available Skills", "", "Load only the skill relevant to the current task; paths are relative to $HORIZON_SKILLS_DIR."]
    for category, title in (("operations", "Horizon Operations"), ("lean", "Lean Formalization"),
                            ("review", "Review"), ("custom", "Project Skills")):
        items = [item for item in index if item["category"] == category]
        if items:
            lines.extend(["", f"## {title}", ""])
            lines.extend(f"- `{item['path']}`: {item['description']}" for item in items)
    return "\n".join(lines) + "\n"


def skill_files(additional_root: Path | None = None) -> dict:
    """Pin skills, descriptors and all installed prompt text in one artifact."""
    root = PIPELINE_ROOT
    sources = [(root / "skills", ""), (root / "subagents", "subagents/")]
    if additional_root is not None:
        if not additional_root.is_dir() or additional_root.is_symlink():
            raise DomainError("skill_source_unavailable", "Configured skill source must be a real directory", 503)
        sources.append((additional_root, ""))
    # Prompt templates belong to the trusted installed control plane. An
    # operator skill addition cannot replace their reserved namespace.
    sources.append((root / "instructions" / "templates", "prompts/"))
    files, total = {}, 0
    for root, prefix in sources:
        for path in sorted(root.rglob("*")):
            relative = prefix + path.relative_to(root).as_posix()
            if path.is_symlink():
                raise DomainError("unsafe_skill_source", "Skill bundles cannot include symbolic links", 422)
            if not path.is_file():
                continue
            if any(part.startswith(".") or part in ("__pycache__", "node_modules") for part in path.relative_to(root).parts):
                continue
            if path.stat().st_size > 8 * 1024**2 or len(files) >= 2048:
                raise DomainError("skill_bundle_too_large", "Skill bundle exceeds its file or byte bound", 422)
            content = path.read_bytes()
            total += len(content)
            if total > 8 * 1024**2:
                raise DomainError("skill_bundle_too_large", "Skill bundle exceeds 8 MiB", 422)
            files[relative] = {"content_base64": base64.b64encode(content).decode("ascii"),
                               "sha256": hashlib.sha256(content).hexdigest(), "executable": bool(path.stat().st_mode & 0o111)}
    entrypoints = sorted(name for name in files if name.endswith("/SKILL.md"))
    index = [_skill_metadata(name, base64.b64decode(files[name]["content_base64"])) for name in entrypoints]
    names = [item["name"] for item in index]
    if len(names) != len(set(names)):
        raise DomainError("duplicate_skill_name", "Each skill must have one discoverable name; override its existing path", 422)
    subagents = []
    for path, item in files.items():
        if not path.startswith("subagents/") or not path.endswith(".md") or len(Path(path).parts) != 3:
            continue
        try:
            row = descriptor(path, base64.b64decode(item["content_base64"]).decode())
        except (ValueError, UnicodeDecodeError, yaml.YAMLError) as error:
            raise DomainError("invalid_subagent_metadata", f"Invalid descriptor in {path}", 422) from error
        if not set(row["skills"]) <= set(names):
            raise DomainError("unknown_subagent_skill", f"Descriptor {path} references an unavailable skill", 422)
        subagents.append({key: value for key, value in row.items() if key != "instructions"})
    if len({row["slug"] for row in subagents}) != len(subagents):
        raise DomainError("duplicate_subagent_name", "Each subagent must have one discoverable name", 422)
    prompts = [{"name": path.removeprefix("prompts/").removesuffix(".md"), "path": path,
                "category": "legacy_prompts" if path.startswith("prompts/legacy/") else "prompts",
                "description": " ".join(base64.b64decode(item["content_base64"]).decode().split())[:240]}
               for path, item in files.items() if path.startswith("prompts/") and path.endswith(".md")]
    for path, text in (("SKILLS.md", skill_overview(index)), ("SUBAGENTS.md", subagent_overview(subagents))):
        content = text.encode()
        total += len(content)
        if total > 8 * 1024**2:
            raise DomainError("skill_bundle_too_large", "Skill bundle exceeds 8 MiB", 422)
        files[path] = {"content_base64": base64.b64encode(content).decode("ascii"),
                       "sha256": hashlib.sha256(content).hexdigest(), "executable": False}
    return {"files": files, "entrypoints": entrypoints, "skill_index": index,
            "subagent_index": subagents, "prompt_index": prompts}


def skill_path(bundle: dict, name: str) -> str:
    """Resolve names inside the pinned bundle instead of assuming a flat layout."""
    matches = [item["path"] for item in bundle.get("skill_index", []) if item["name"] == name]
    if not matches:
        matches = [path for path in bundle.get("entrypoints", []) if Path(path).parent.name == name]
    if len(matches) != 1 or matches[0] not in bundle.get("files", {}):
        raise DomainError("skill_unavailable", f"Pinned catalog must contain exactly one {name} skill", 422)
    return matches[0]
