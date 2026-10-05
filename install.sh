#!/usr/bin/env bash
#
# Archon Horizon installer.
#
#   curl -sSL https://raw.githubusercontent.com/frenzymath/Archon-Horizon/refs/heads/main/install.sh | bash
#
# Downloads the latest main tarball and installs the control plane.
# Override ORG/REPO/BRANCH via env to
# install from a fork or branch.

set -euo pipefail

ORG="${ARCHON_HORIZON_ORG:-frenzymath}"
REPO="${ARCHON_HORIZON_REPO:-Archon-Horizon}"
BRANCH="${ARCHON_HORIZON_BRANCH:-main}"

red()   { printf '\033[31m%s\033[0m\n' "$1"; }
purple() { printf '\033[35m%s\033[0m\n' "$1"; }

if ! python3 -m pip --version >/dev/null 2>&1; then
    red "Error: pip is not available. See https://pip.pypa.io/en/stable/installation/"
    exit 1
fi

INSTALL_SCRATCH="${TMPDIR:-$HOME/.horizon/development-tmp}"
mkdir -p "$INSTALL_SCRATCH"
TEMP_DIR="$(mktemp -d "$INSTALL_SCRATCH/horizon-install.XXXXXX")"
cleanup() { rm -rf "$TEMP_DIR"; }
trap cleanup EXIT

cd "$TEMP_DIR"

purple "Downloading Archon Horizon (${ORG}/${REPO}@${BRANCH})..."
curl -fL "https://github.com/${ORG}/${REPO}/archive/refs/heads/${BRANCH}.tar.gz" -o archon-horizon.tar.gz
mkdir source
tar -xzf archon-horizon.tar.gz -C source --strip-components=1
cd source

# Source archives do not contain the generated dashboard assets.
if ! command -v npm >/dev/null 2>&1; then
    red "Error: Node.js and npm are required to build the dashboard from source."
    exit 1
fi

purple "Building the dashboard..."
npm --prefix src/archon_horizon/frontend ci
npm --prefix src/archon_horizon/frontend run build

purple "Installing the archon-horizon package..."
python3 -m pip install '.[control-plane]'

purple "Installed. Configure PostgreSQL, then run 'horizon --config /absolute/path/server.json init --interactive'."
purple "See docs/pipeline-setup.md for database initialization and worker enrollment."
