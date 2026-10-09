# Documentation and review tools

The repository's Markdown guides build with MkDocs. Root README and contribution
instructions are included directly, so they do not acquire separate stale copies.
Repository source links point to the build's Git revision on GitHub. Runtime
installation does not require the documentation toolchain.

## Build and preview

Use the development virtual environment and installed frontend dependencies:

```sh
python -m pip install -r requirements-docs.txt
python scripts/generate_dashboard_fixture.py --check
python -m mkdocs build --strict
npm --prefix src/archon_horizon/frontend run build:demo
python scripts/assemble_docs_demo.py
npm --prefix src/archon_horizon/frontend run test:demo
python -m http.server 8000 --bind 127.0.0.1 --directory _site
```

Open `http://127.0.0.1:8000/` or `/demo/`. The frontend demo uses relative asset
URLs and keeps navigation on its current path, including GitHub Pages' repository
subpath. The browser check serves the artifact below `/Archon-Horizon/demo/` and
checks navigation, reload, graph rendering, read-only behavior, and absence of
API/external network requests. `build:demo` leaves the normal packaged dashboard
in `frontend/dist/` untouched.

After editing the synthetic example, run
`python scripts/generate_dashboard_fixture.py`, then repeat the build and browser
check. The generated JSON is versioned and the generator's `--check` detects drift.

## GitHub Pages

The Documentation workflow builds and uploads an artifact for pull requests and
pushes to `main`. To publish a reviewed build, select **GitHub Actions** as the
repository's Pages source, then run **Documentation** on `main` with `publish`
enabled. The deploy job uses the `github-pages` environment and the minimum Pages
permissions. Ordinary PR/push builds do not deploy. The expected URL is
`https://frenzymath.github.io/Archon-Horizon/`; a different repository name requires
updating `site_url` in `mkdocs.yml`.

## Review inventory

`scripts/review_inventory.py` reads Python syntax without importing application
code or contacting an installation:

```sh
python scripts/review_inventory.py --format markdown --output "$TMPDIR/review-inventory.md"
python scripts/review_inventory.py --output "$TMPDIR/review-inventory.json"
python scripts/review_inventory.py --coverage coverage.json --review-ledger review.json
```

It lists every Python file and function in the selected source tree, including
nested functions and methods, with lines, docstrings, comments, branch counts and
nesting. Pass `--source scripts` to review development tools. The default source
is `src/archon_horizon`; generated dependency/build directories are excluded.

Optional `--coverage` accepts coverage.py JSON line data. Coverage is reported as
observed executable lines, not branch coverage or proof of correctness. Optional
`--review-ledger` accepts explicit reviewed function names:

```json
{
  "scripts/review_inventory.py": {
    "reviewed_functions": ["scan", "main"],
    "notes": "Read source and checked malformed-input behavior."
  }
}
```

Missing evidence stays unknown. Function names include their class/parent scope
and line number distinguishes same-name definitions. Metrics identify useful
review targets; they do not determine whether a function should exist or whether
its behavior is correct. Record concrete findings and validation in the review
notes, including any unreviewed scope.
