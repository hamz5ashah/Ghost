#!/usr/bin/env bash
# ghost-wrapper.sh — symlinked to ~/.local/bin/ghost.
# Activates the managed venv and execs the real ghost CLI.

GHOST_HOME="${GHOST_HOME:-$HOME/.ghost}"
VENV="$GHOST_HOME/.venv"

if [[ ! -d "$VENV" ]]; then
    echo "ghost: venv not found at $VENV" >&2
    echo "Re-run the installer: curl -fsSL https://hamz5ashah.github.io/Ghost/install.sh | bash" >&2
    exit 1
fi

exec "$VENV/bin/ghost" "$@"
