"""Bundled capability skills (know-how the agents consult on demand)."""

from __future__ import annotations

from .registry import Skill, available_skills, install_skills

__all__ = ["Skill", "available_skills", "install_skills"]
