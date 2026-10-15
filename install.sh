#!/usr/bin/env bash
# OpenCortyx installer for macOS, Linux and WSL.
#   curl -fsSL https://raw.githubusercontent.com/opencortyx/OpenCortyx/main/install.sh | bash
set -euo pipefail

REPO="${OPENCORTYX_REPO:-https://github.com/opencortyx/OpenCortyx.git}"
BRANCH="${OPENCORTYX_BRANCH:-}"
DIR="${OPENCORTYX_HOME:-$HOME/opencortyx}"

info() { printf '==> %s\n' "$*"; }
die()  { printf '\033[31mx  %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "Install Docker first: https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1 || die "Docker is not running. Start it and re-run the installer."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."
command -v curl >/dev/null 2>&1 || die "curl is required."

here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$here" ] && [ -f "$here/compose.yaml" ] && [ -x "$here/bin/opencortyx" ]; then
  DIR="$here"
  info "Using checkout at $DIR"
  "$DIR/bin/opencortyx" init
  "$DIR/bin/opencortyx" up
elif [ -d "$DIR/.git" ]; then
  info "Updating $DIR"
  "$DIR/bin/opencortyx" init
  "$DIR/bin/opencortyx" update
else
  command -v git >/dev/null 2>&1 || die "git is required to download OpenCortyx."
  if [ -z "$BRANCH" ]; then
    BRANCH="$(git ls-remote --tags --refs "$REPO" 'v*' 2>/dev/null | awk -F/ '{print $NF}' \
      | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1 || true)"
    BRANCH="${BRANCH:-main}"
  fi
  info "Downloading OpenCortyx $BRANCH to $DIR"
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$DIR"
  chmod +x "$DIR/bin/opencortyx"
  "$DIR/bin/opencortyx" init
  "$DIR/bin/opencortyx" up
fi

bin_dir="$HOME/.local/bin"
mkdir -p "$bin_dir"
ln -sf "$DIR/bin/opencortyx" "$bin_dir/opencortyx"
case ":$PATH:" in
  *":$bin_dir:"*) info "Installed the 'opencortyx' command" ;;
  *) info "Installed 'opencortyx' to $bin_dir (add it to your PATH)" ;;
esac
