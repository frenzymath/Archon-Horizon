"""Explicitly configured, PostgreSQL-backed Horizon control plane.

The root contains entrypoints, configuration, authentication and shared request
contracts. Implementation modules live in packages named for their responsibility:
review, integrations, missions, execution, dashboard, instructions, persistence,
projects, operations, providers and worker. Import concrete modules explicitly;
package initializers do not load server or optional scientific dependencies.

Bundled skills, subagent descriptors and applied migrations retain stable paths.
See docs/architecture.md for the layout and src/archon_horizon/pipeline/_resources.py
for asset locations used by relocated loaders.
"""
