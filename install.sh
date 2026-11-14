#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${GH_SETUP_REPO_URL:-https://github.com/lispz1610/github-repo-setup.git}"
INSTALL_DIR="${GH_SETUP_INSTALL_DIR:-$HOME/.local/share/gh-setup}"
BIN_DIR="${GH_SETUP_BIN_DIR:-$HOME/.local/bin}"

# --- Runtime Preflight Checks ---
# 1. Python 3 (>= 3.10)
if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: Python 3 (version 3.10 or newer) is required but python3 was not found in PATH." >&2
    echo "Please install Python 3.10+ before installing gh-setup." >&2
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
    echo "Please install git before installing gh-setup." >&2
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
    echo "Please run 'gh auth login' or export a valid GH_TOKEN before running gh-setup." >&2
    exit 1
fi

# --- Installation & Upgrades ---
mkdir -p "$BIN_DIR"

if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating existing gh-setup installation at $INSTALL_DIR..."
    git -C "$INSTALL_DIR" pull --ff-only || {
        echo "Warning: git pull failed, using existing files."
    }
else
    echo "Cloning gh-setup into $INSTALL_DIR..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

if [ -f "$INSTALL_DIR/gh-setup" ]; then
    chmod +x "$INSTALL_DIR/gh-setup"
    ln -sf "$INSTALL_DIR/gh-setup" "$BIN_DIR/gh-setup"
fi

echo "gh-setup installed successfully to $BIN_DIR/gh-setup"

# Post-install verification preflights: --help and doctor
if ! "$BIN_DIR/gh-setup" --help >/dev/null 2>&1; then
    echo "Error: Post-install verification failed for 'gh-setup --help'." >&2
    exit 1
fi

if ! "$BIN_DIR/gh-setup" doctor >/dev/null 2>&1; then
    echo "Error: Post-install diagnostics failed for 'gh-setup doctor'." >&2
    exit 1
fi

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    echo "Notice: $BIN_DIR is not in your PATH."
    echo "You can add it by running:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
elif [[ "$(command -v gh-setup || true)" != "$BIN_DIR/gh-setup" ]]; then
    echo ""
    echo "Notice: $BIN_DIR is not first in your PATH for gh-setup."
    echo "You can give the installed command precedence by running:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
fi
