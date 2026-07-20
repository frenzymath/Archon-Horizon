# Archon Horizon Demo

This is a deliberately small, non-package workspace used by the public
dashboard demo. It contains one tiny Lean project, two blueprint chapters,
partial formalization coverage, and two synthetic runs showing both Claude Code
and Codex transcripts. The files are fixtures for the dashboard, not a second
test suite or a dependency of the Python package.

Run it locally from the repository root:

```bash
npm --prefix src/archon_horizon/frontend ci
npm --prefix src/archon_horizon/frontend run build
PYTHONPATH=src .venv/bin/python demo/prepare_ledger.py
PYTHONPATH=src .venv/bin/horizon --root demo dashboard --static \
  --out /tmp/archon-horizon-demo --dist src/archon_horizon/frontend/dist
python -m http.server 8000 --directory /tmp/archon-horizon-demo
```

The GitHub Pages workflow rebuilds the same snapshot on every push to `main`.
