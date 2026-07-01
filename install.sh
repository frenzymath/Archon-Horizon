#!/usr/bin/env bash
#
# Archon Horizon installer.
#
#   curl -sSL https://raw.githubusercontent.com/frenzymath/Archon-Horizon/refs/heads/main/install.sh | bash
#
# Downloads the latest main tarball, installs the package, and runs `horizon
# setup` to check/install external tools. Override ORG/REPO/BRANCH via env to
# install from a fork or branch.

set -euo pipefail

ORG="${ARCHON_HORIZON_ORG:-frenzymath}"
REPO="${ARCHON_HORIZON_REPO:-Archon-Horizon}"
BRANCH="${ARCHON_HORIZON_BRANCH:-main}"

red()   { printf '\033[31m%s\033[0m\n' "$1"; }
green() { printf '\033[32m%s\033[0m\n' "$1"; }

if ! python3 -m pip --version >/dev/null 2>&1; then
    red "Error: pip is not available. See https://pip.pypa.io/en/stable/installation/"
    exit 1
fi

TEMP_DIR="$(mktemp -d)"
cleanup() { rm -rf "$TEMP_DIR"; }
trap cleanup EXIT

cd "$TEMP_DIR"

green "Downloading Archon Horizon (${ORG}/${REPO}@${BRANCH})..."
curl -fL "https://github.com/${ORG}/${REPO}/archive/refs/heads/${BRANCH}.tar.gz" -o archon-horizon.tar.gz
tar -xzf archon-horizon.tar.gz
cd "${REPO}-${BRANCH}"

# The dashboard SPA (frontend/dist) is a build artifact — it is gitignored and so
# absent from the source tarball. Build it here (best-effort) so pip packages it
# into the installed dashboard, including the run-logs viewer. Without Node the
# install still succeeds; `horizon dashboard --static` then falls back to the
# read-only Python page (no logs) until the SPA is built.
if command -v npm >/dev/null 2>&1; then
    green "Building the dashboard SPA..."
    ( npm --prefix src/archon_horizon/frontend install \
      && npm --prefix src/archon_horizon/frontend run build ) \
      || red "Frontend build failed; the dashboard will use the read-only fallback."
else
    red "Node/npm not found; skipping dashboard build (the dashboard will use the read-only fallback)."
fi

green "Installing the archon-horizon package..."
python3 -m pip install .

green "Running 'horizon setup' to check external tools..."
horizon setup || true

green "Done. Run 'horizon init' in a workspace to get started."
