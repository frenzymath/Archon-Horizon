"""Read-only views of the installed instructions, separate from project overrides."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from .bundles import skill_files
from ..errors import DomainError
from .subagent_guidance import descriptor


def read_catalog(additional_root: Path | None = None, *, path: str | None = None) -> dict:
    bundle = skill_files(additional_root)
    reviewers = [row for row in bundle["subagent_index"] if row["category"] == "reviewers"]
    if path is not None:
        item = bundle["files"].get(path)
        if item is None:
            raise DomainError("instruction_not_found", "Instruction file is not in the installed catalog", 404)
        content = base64.b64decode(item["content_base64"])
        if len(content) > 512 * 1024:
            raise DomainError("instruction_too_large", "Instruction preview exceeds 512 KiB", 413)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DomainError("instruction_not_text", "This catalog resource is not a text file", 422) from error
        reviewer = next((row for row in reviewers if row["source_path"] == path), None)
        subagent = next((row for row in bundle["subagent_index"] if row["source_path"] == path), None)
        instructions = descriptor(path, text, purpose="review" if reviewer else "task")["instructions"] if subagent else None
        if reviewer:
            contract = bundle["files"]["review/horizon-review/references/review-contract.md"]
            instructions = base64.b64decode(contract["content_base64"]).decode().strip() + "\n\n" + instructions
        return {"path": path, "content": text, "instructions": instructions,
                "sha256": item["sha256"], "executable": item["executable"]}
    index = []
    for item in bundle["skill_index"]:
        prefix = item["path"].removesuffix("SKILL.md")
        index.append({**item, "resources": [name for name in bundle["files"]
                     if name.startswith(prefix) and name != item["path"]]})
    digest = hashlib.sha256("\n".join(f"{name}:{item['sha256']}"
                            for name, item in sorted(bundle["files"].items())).encode()).hexdigest()
    return {"revision": digest, "skills": index, "files": sorted(bundle["files"]), "prompts": bundle["prompt_index"],
            "subagents": bundle["subagent_index"],
            "reviewers": [{key: row[key] for key in ("slug", "description", "skills", "source_path")}
                          for row in reviewers]}
