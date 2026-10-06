"""Discovery metadata for pinned specialist and reviewer instruction files."""

from pathlib import PurePosixPath

import yaml


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
            "instructions": body.strip() + "\n\nRelevant skills: " + ", ".join(metadata["skills"])
            + ". Resolve them in $HORIZON_SKILLS_DIR/SKILLS.md; "
            f"load the procedures needed for this {purpose}, not the entire catalog."}


def overview(index: list[dict]) -> str:
    lines = ["# Available Subagents", "",
             "Optional task specializations, not permissions or automatic dispatch rules.",
             "Adapt these advisory descriptions or write your own when the task calls for a different perspective.",
             "Read the selected descriptor relative to $HORIZON_SKILLS_DIR and include its body",
             "in the native child's prompt with the bounded task, source revision, file ownership,",
             "skill directory and expected result. Follow horizon-delegation. For formal PR",
             "reviews, use the project's enabled reviewer and prepared invocation workflow."]
    for category in dict.fromkeys(item["category"] for item in index):
        lines.extend(["", f"## {category.replace('_', ' ').title()}", ""])
        lines.extend(f"- `{item['source_path']}`: {item['description']}"
                     for item in index if item["category"] == category)
    return "\n".join(lines) + "\n"
