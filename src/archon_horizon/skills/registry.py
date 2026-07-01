"""Enumerate and install the bundled capability skills.

Skills are plain Markdown with YAML frontmatter (``name``, ``description``),
one per ``<name>/SKILL.md`` directory beside this module. They hold capability
know-how (lean checks, the dependency DAG, blueprint conventions, inbox actions)
so the base prompt stays lean and the detail is loaded on demand. ``install_skills``
copies them to ``<root>/.claude/skills/`` where Claude Code surfaces them; on
other engines an agent can read the same files directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SKILLS_ROOT = Path(__file__).parent


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str


def _parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            out[key.strip()] = value.strip()
    return out


def _skill_dirs() -> list[Path]:
    return sorted(
        d for d in SKILLS_ROOT.iterdir() if d.is_dir() and (d / "SKILL.md").exists()
    )


def available_skills() -> list[Skill]:
    skills: list[Skill] = []
    for directory in _skill_dirs():
        fm = _parse_frontmatter((directory / "SKILL.md").read_text("utf-8"))
        skills.append(Skill(name=fm.get("name", directory.name), description=fm.get("description", "")))
    return skills


def install_skills(root: Path, *, overwrite=None) -> list[str]:
    """Copy bundled skills into ``<root>/.claude/skills/<name>/SKILL.md``.

    ``overwrite`` is an optional callback ``(name, dest_path, new_text) -> bool``
    consulted only when the destination already exists **and differs** from the
    bundled version — so reinit can offer keep-vs-overwrite without prompting for
    unchanged files. When ``None`` (default) an existing, differing skill is
    overwritten. Returns the names actually written.
    """
    installed: list[str] = []
    for directory in _skill_dirs():
        dest_dir = root / ".claude" / "skills" / directory.name
        dest = dest_dir / "SKILL.md"
        new_text = (directory / "SKILL.md").read_text("utf-8")
        if dest.exists():
            if dest.read_text("utf-8") == new_text:
                continue
            if overwrite is not None and not overwrite(directory.name, dest, new_text):
                continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_text(new_text, "utf-8")
        installed.append(directory.name)
    return installed
