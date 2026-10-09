"""Locate bundled assets independently of the module that consumes them.

Skills, descriptors, and Alembic revisions retain their installed paths when
Python internals move into subpackages. Resolving them here avoids accidentally
looking for a second asset tree beside a relocated loader. These are package
resources, not operator configuration or live installation discovery.
"""

from pathlib import Path

PIPELINE_ROOT = Path(__file__).parent
SKILLS_ROOT = PIPELINE_ROOT / "skills"
SUBAGENTS_ROOT = PIPELINE_ROOT / "subagents"
MIGRATIONS_ROOT = PIPELINE_ROOT / "migrations"
