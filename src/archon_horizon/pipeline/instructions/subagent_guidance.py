"""Discovery metadata for pinned specialist and reviewer instruction files."""

from pathlib import PurePosixPath

import yaml

from .templates import template


def descriptor(path: str, source: str, *, purpose: str = "task") -> dict:
    location = PurePosixPath(path)
    header, separator, body = source.removeprefix("---\n").partition("\n---\n")
    metadata = yaml.safe_load(header) if source.startswith("---\n") and separator else None
    if (len(location.parts) != 3 or location.parts[0] != "subagents"
            or not isinstance(metadata, dict) or metadata.get("name") != location.stem
            or not isinstance(metadata.get("description"), str) or not metadata["description"].strip()
            or not isinstance(metadata.get("skills"), list)
            or not all(isinstance(skill, str) and skill.strip() for skill in metadata["skills"])
            or not body.strip()):
        raise ValueError(f"Invalid subagent descriptor: {path}")
    return {"slug": metadata["name"], "description": metadata["description"],
            "category": location.parts[1], "skills": metadata["skills"], "source_path": path,
            "instructions": body.strip() + "\n\n" + template("subagent-skills",
                skills=", ".join(metadata["skills"]), purpose=purpose)}


def overview(index: list[dict]) -> str:
    lines = [template("subagent-overview")]
    for category in dict.fromkeys(item["category"] for item in index):
        lines.extend(["", f"## {category.replace('_', ' ').title()}", ""])
        lines.extend(f"- `{item['source_path']}`: {item['description']}"
                     for item in index if item["category"] == category)
    return "\n".join(lines) + "\n"
