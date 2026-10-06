# Third-Party Notices

Archon Horizon is licensed under the Apache License 2.0 (see `LICENSE`).
The following records describe historical imports, distributed dashboard
dependencies, and external tools.

## Dashboard Dependencies

The built dashboard distributed with Horizon includes JavaScript, WebAssembly,
fonts, and styles from its frontend dependencies. The dependency declarations
and resolved versions are recorded in `src/archon_horizon/frontend/package.json`
and `package-lock.json`; each component retains its own license.

## Runtime dependencies (not bundled)

These are installed/launched at runtime and are not redistributed in this
repository. Each remains under its own license.

### lean-lsp-mcp

- Upstream: https://github.com/oOo0oOo/lean-lsp-mcp
- License: MIT — Copyright (c) 2025 Oliver Dressler
- How it is used: launched on demand via `uvx lean-lsp-mcp` and exposed to
  agents through managed harness invocation settings. Horizon does not rewrite
  the checkout's `.mcp.json`. Not vendored.

### Python dependencies

Installed from PyPI per `pyproject.toml` (`PyYAML`, `typer`, `click`, `rich`,
`pyfiglet`, `numpy`, `scipy`, `bm25s`) under their respective licenses.
Lean declaration ranking uses [bm25s](https://github.com/xhluca/bm25s)
(MIT) with NumPy/SciPy sparse matrices; English stemming is not applied.

The opt-in pipeline runtime installs FastAPI, Uvicorn, Pydantic, SQLAlchemy,
Psycopg, Alembic, HTTPX, Tenacity, argon2-cffi, Pybtex, python-stdnum, and the
OpenTelemetry Python API/SDK (Apache-2.0) from
PyPI under their respective licenses. PostgreSQL and Podman are external
services/tools, not vendored code. The pipeline dashboard includes TanStack
Query under its upstream license; resolved frontend dependencies are recorded
in the lockfile above.

### Lean / Mathlib

Lean 4, Lake, and Mathlib are external toolchains the agents drive; they are
provisioned by the user (e.g. via `elan`) and are not redistributed here.
