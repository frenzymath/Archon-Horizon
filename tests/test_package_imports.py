"""Every shipped Python entrypoint must import without retired runtime modules."""

from __future__ import annotations

import importlib
from pathlib import Path
import subprocess
import sys

import archon_horizon


def test_all_shipped_modules_import():
    package = Path(archon_horizon.__file__).parent
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package)
        if "frontend" in relative.parts:
            continue
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        importlib.import_module(".".join(["archon_horizon", *parts]))


def test_pipeline_packages_keep_lightweight_contract_imports_available():
    """Package initialization must not turn a worker contract into a server import."""
    code = """
import importlib
import importlib.abc
import sys

class NoServices(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'sqlalchemy', 'psycopg', 'alembic', 'fastapi', 'numpy', 'scipy', 'bm25s'}:
            raise RuntimeError('unexpected service dependency: ' + fullname)

sys.meta_path.insert(0, NoServices())
for area in ('review', 'integrations', 'missions', 'execution', 'dashboard',
             'instructions', 'persistence', 'projects', 'operations', 'providers'):
    importlib.import_module('archon_horizon.pipeline.' + area)
from archon_horizon.pipeline.models import AssignmentCreate
from archon_horizon.pipeline.review.contracts import ReviewAssessment
"""
    result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
