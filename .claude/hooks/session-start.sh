#!/bin/bash
# SessionStart hook (lightweight) — installs the tools needed to validate the
# GitOps manifests in Claude Code on the web sessions:
#   - kubectl   → `kubectl kustomize <overlay>` builds the Flux overlays
#   - yamllint  → lints the YAML
#
# Requires the environment's network policy to allow outbound access to
# dl.k8s.io and pypi.org (or the Ubuntu apt mirrors). If egress is blocked,
# the installs below fail.
set -euo pipefail

# Only run in the remote (web) environment.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Pick a writable bin dir (root container → /usr/local/bin; else ~/.local/bin).
BIN_DIR="/usr/local/bin"
if [ ! -w "$BIN_DIR" ]; then
  BIN_DIR="$HOME/.local/bin"
  mkdir -p "$BIN_DIR"
  case ":$PATH:" in *":$BIN_DIR:"*) ;; *) export PATH="$BIN_DIR:$PATH" ;; esac
fi

# 1. kubectl (bundles `kubectl kustomize`). Idempotent.
if ! command -v kubectl >/dev/null 2>&1; then
  echo "Installing kubectl..."
  ver="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"
  curl -fsSLo "$BIN_DIR/kubectl" "https://dl.k8s.io/release/${ver}/bin/linux/amd64/kubectl"
  chmod +x "$BIN_DIR/kubectl"
fi

# 2. yamllint. Prefer pip, fall back to apt. Idempotent.
if ! command -v yamllint >/dev/null 2>&1; then
  echo "Installing yamllint..."
  if command -v pip3 >/dev/null 2>&1; then
    pip3 install --user yamllint
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y yamllint
  else
    echo "⚠ Could not install yamllint automatically."
  fi
fi

echo "✅ Toolchain ready (kubectl, yamllint). Validate overlays with:"
echo "   for o in base local management workload; do kubectl kustomize apps/flux/\$o >/dev/null && echo \"ok: \$o\"; done"
