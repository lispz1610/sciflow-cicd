#!/usr/bin/env bash
set -euo pipefail

DEFAULT_REPO_URL="https://github.com/lispz1610/sciflow-cicd.git"
DEFAULT_INSTALL_DIR="${HOME}/.local/share/sciflow-cicd"
DEFAULT_BIN_DIR="${HOME}/.local/bin"

REPO_URL="${SCIFLOW_CICD_REPO_URL:-$DEFAULT_REPO_URL}"
INSTALL_DIR="${SCIFLOW_CICD_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
BIN_DIR="${SCIFLOW_CICD_BIN_DIR:-$DEFAULT_BIN_DIR}"
BIN_LINK="${BIN_DIR}/sciflow-cicd"

# --- Runtime Preflight Checks ---
# 1. Python 3 (>= 3.10)
if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: Python 3 (version 3.10 or newer) is required but python3 was not found in PATH." >&2
    echo "Please install Python 3.10+ before installing sciflow-cicd." >&2
    exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "Error: Python 3.10 or newer is required. Detected: $(python3 --version 2>&1)" >&2
    echo "Please upgrade Python to version 3.10 or higher." >&2
    exit 1
fi

# 2. Git
if ! command -v git >/dev/null 2>&1; then
    echo "Error: git is required but was not found in PATH." >&2
    echo "Please install git before installing sciflow-cicd." >&2
    exit 1
fi

# 3. GitHub CLI (gh) and authentication
if ! command -v gh >/dev/null 2>&1; then
    echo "Error: GitHub CLI (gh) is required but was not found in PATH." >&2
    echo "Please install gh: https://cli.github.com" >&2
    exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
    echo "Error: GitHub CLI (gh) is not authenticated." >&2
    echo "Please run 'gh auth login' or export a valid GH_TOKEN before running sciflow-cicd." >&2
    exit 1
fi

# --- Installation & Upgrades ---
mkdir -p "$BIN_DIR"

if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating existing sciflow-cicd installation at $INSTALL_DIR..."
    git -C "$INSTALL_DIR" pull --ff-only || {
        echo "Warning: git pull failed, using existing files."
    }
elif [ -e "$INSTALL_DIR" ]; then
    echo "Error: $INSTALL_DIR already exists without a .git repository." >&2
    echo "Remove or move $INSTALL_DIR, or set SCIFLOW_CICD_INSTALL_DIR to a different path." >&2
    exit 1
else
    echo "Cloning sciflow-cicd into $INSTALL_DIR..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

if [ -f "$INSTALL_DIR/sciflow-cicd" ]; then
    chmod +x "$INSTALL_DIR/sciflow-cicd"
    ln -sf "$INSTALL_DIR/sciflow-cicd" "$BIN_LINK"
fi

echo "sciflow-cicd installed successfully to $BIN_LINK"

# Post-install verification preflights: --help and doctor
if ! "$BIN_LINK" --help >/dev/null 2>&1; then
    echo "Error: Post-install verification failed for 'sciflow-cicd --help'." >&2
    exit 1
fi

if ! doctor_output="$("$BIN_LINK" doctor 2>&1)"; then
    if [ -n "$doctor_output" ]; then
        echo "$doctor_output" >&2
    fi
    echo "Error: Post-install diagnostics failed for 'sciflow-cicd doctor'." >&2
    exit 1
fi

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    echo "Notice: $BIN_DIR is not in your PATH."
    echo "You can add it by running:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
elif [[ "$(command -v sciflow-cicd || true)" != "$BIN_LINK" ]]; then
    echo ""
    echo "Notice: $BIN_DIR is not first in your PATH for sciflow-cicd."
    echo "You can give the installed command precedence by running:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
fi
