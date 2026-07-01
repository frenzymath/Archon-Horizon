# Third-Party Notices

Archon Horizon is licensed under the Apache License 2.0 (see `LICENSE`). It
vendors and interoperates with the third-party components listed below.

## Vendored in-repo

### leandag

- Upstream: https://github.com/AxelDlv00/LeanDAG (v0.1.0)
- Local path: `src/archon_horizon/leandag/`
- Notes: The dependency-graph engine is vendored so it ships in-repo with no
  external git install, with minimal local edits noted in its `__init__.py`.

## Runtime dependencies (not bundled)

These are installed/launched at runtime and are not redistributed in this
repository. Each remains under its own license.

### lean-lsp-mcp

- Upstream: https://github.com/oOo0oOo/lean-lsp-mcp
- License: MIT — Copyright (c) 2025 Oliver Dressler
- How it is used: launched on demand via `uvx lean-lsp-mcp` and exposed to
  agents through the workspace `.mcp.json`. Not vendored.

### Python dependencies

Installed from PyPI per `pyproject.toml` (`PyYAML`, `typer`, `rich`,
`pyfiglet`) under their respective licenses.

### Lean / Mathlib

Lean 4, Lake, and Mathlib are external toolchains the agents drive; they are
provisioned by the user (e.g. via `elan`) and are not redistributed here.
