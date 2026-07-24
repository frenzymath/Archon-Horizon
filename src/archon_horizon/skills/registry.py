"""Enumerate and install the bundled capability skills.

Skills are plain Markdown with YAML frontmatter (``name``, ``description``),
one per ``<name>/SKILL.md`` directory beside this module. They hold capability
know-how (lean checks, the dependency DAG, blueprint conventions, inbox actions)
so the base prompt stays lean and the detail is loaded on demand. ``install_skills``
copies them to ``<root>/.claude/skills/`` where Claude Code surfaces them; on
other engines an agent can read the same files directly.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

SKILLS_ROOT = Path(__file__).parent

# Skills that Horizon used to ship and has since dropped. ``install_skills`` only
# ever *writes* bundled skills, so without this a retired skill lives on in every
# existing workspace and keeps teaching a removed workflow. Pruning is by explicit
# name — never "anything not bundled" — so a workspace's own custom skills survive.
_RETIRED_SKILLS = ("horizon-commit", "leandag")


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


def retired_skills(root: Path) -> list[str]:
    """Return explicitly retired skill directories still installed in ``root``."""
    return [
        name for name in _RETIRED_SKILLS
        if (root / ".claude" / "skills" / name).is_dir()
    ]


def stale_skills(root: Path) -> list[str]:
    """Bundled skills whose copy is stale, plus installed retired skills.

    Skills are written to a workspace only by ``init`` / ``horizon skills
    install``, so a workspace freezes its guidance at install time while the
    package moves on — agents then follow text that no longer matches the tools.
    This reports the drift; it never writes, because the `horizon` skill is
    advertised as per-workspace editable, so a difference here may be a
    deliberate local edit rather than staleness. The caller decides.
    """
    out: list[str] = []
    for directory in _skill_dirs():
        dest = root / ".claude" / "skills" / directory.name / "SKILL.md"
        try:
            if dest.read_text("utf-8") == (directory / "SKILL.md").read_text("utf-8"):
                continue
        except OSError:
            pass  # missing (or unreadable) counts as stale
        out.append(directory.name)
    # A retired skill is stale by definition. Keep this explicit allow-list so
    # user-authored skills are never treated as obsolete by accident.
    out.extend(retired_skills(root))
    return out


def install_skills(root: Path, *, overwrite=None) -> list[str]:
    """Copy bundled skills into ``<root>/.claude/skills/<name>/SKILL.md``.

    ``overwrite`` is an optional callback ``(name, dest_path, new_text) -> bool``
    consulted only when the destination already exists **and differs** from the
    bundled version — so reinit can offer keep-vs-overwrite without prompting for
    unchanged files. When ``None`` (default) an existing, differing skill is
    overwritten. Returns the names actually written.

    Retired skills (``_RETIRED_SKILLS``) are deleted from the workspace so an
    upgrade doesn't leave a removed workflow documented alongside the current one.
    """
    for name in _RETIRED_SKILLS:
        stale = root / ".claude" / "skills" / name
        if stale.is_dir():
            shutil.rmtree(stale)

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
