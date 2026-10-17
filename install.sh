#!/usr/bin/env bash
# OpenCortyx installer for macOS, Linux and WSL.
#   curl -fsSL https://raw.githubusercontent.com/opencortyx/OpenCortyx/main/install.sh | bash
# Beta releases:
#   curl -fsSL https://raw.githubusercontent.com/opencortyx/OpenCortyx/main/install.sh | OPENCORTYX_CHANNEL=beta bash
set -euo pipefail

REPO="${OPENCORTYX_REPO:-https://github.com/opencortyx/OpenCortyx.git}"
BRANCH="${OPENCORTYX_BRANCH:-}"
CHANNEL="${OPENCORTYX_CHANNEL:-stable}"
DIR="${OPENCORTYX_HOME:-$HOME/opencortyx}"

info() { printf '==> %s\n' "$*"; }
die()  { printf '\033[31mx  %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "Install Docker first: https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1 || die "Docker is not running. Start it and re-run the installer."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."
command -v curl >/dev/null 2>&1 || die "curl is required."
case "$CHANNEL" in stable|beta) ;; *) die "OPENCORTYX_CHANNEL must be stable or beta, not '$CHANNEL'." ;; esac

newest_tag() {
  # Newest tag matching $1. sort -V ranks v1.0.0-beta.2 above v1.0.0; "~" sorts before everything,
  # so it stands in for the "-" while sorting.
  { grep -E "$1" || true; } | sed 's/-/~/' | sort -V | sed 's/~/-/' | tail -1
}

# The command goes on PATH BEFORE the stack is started: a first start that fails (Docker short of
# memory, an image that will not pull) is exactly when `opencortyx up` / `doctor` is needed next,
# and installing the command last left it missing then.
install_command() {
  bin_dir="$HOME/.local/bin"
  mkdir -p "$bin_dir"
  ln -sf "$DIR/bin/opencortyx" "$bin_dir/opencortyx"
  case ":$PATH:" in
    *":$bin_dir:"*) info "Installed the 'opencortyx' command" ;;
    *) info "Installed 'opencortyx' to $bin_dir (add it to your PATH)" ;;
  esac
}

here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$here" ] && [ -f "$here/compose.yaml" ] && [ -x "$here/bin/opencortyx" ]; then
  DIR="$here"
  info "Using checkout at $DIR"
  install_command
  "$DIR/bin/opencortyx" init
  "$DIR/bin/opencortyx" up
elif [ -d "$DIR/.git" ]; then
  info "Updating $DIR"
  install_command
  "$DIR/bin/opencortyx" init
  "$DIR/bin/opencortyx" update
else
  command -v git >/dev/null 2>&1 || die "git is required to download OpenCortyx."
  if [ -z "$BRANCH" ]; then
    tags="$(git ls-remote --tags --refs "$REPO" 'v*' 2>/dev/null | awk -F/ '{print $NF}' || true)"
    stable='^v[0-9]+\.[0-9]+\.[0-9]+$' beta='^v[0-9]+\.[0-9]+\.[0-9]+(-beta\.[0-9]+)?$'
    if [ "$CHANNEL" = stable ]; then
      BRANCH="$(printf '%s\n' "$tags" | newest_tag "$stable")"
      if [ -z "$BRANCH" ]; then
        BRANCH="$(printf '%s\n' "$tags" | newest_tag "$beta")"
        [ -z "$BRANCH" ] || { CHANNEL=beta; info "No stable release yet: installing beta $BRANCH"; }
      fi
    else
      BRANCH="$(printf '%s\n' "$tags" | newest_tag "$beta")"
    fi
    BRANCH="${BRANCH:-main}"
  fi
  export OPENCORTYX_CHANNEL="$CHANNEL"
  info "Downloading OpenCortyx $BRANCH to $DIR"
  existed=""; [ -e "$DIR" ] && existed=1
  # A half-written checkout would send the next run down the "Updating" path. Removed only when this
  # run created the folder, never a folder that was already there.
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$DIR" || {
    [ -n "$existed" ] || rm -rf "$DIR"
    die "Downloading OpenCortyx failed (see the git message above)."
  }
  chmod +x "$DIR/bin/opencortyx"
  install_command
  "$DIR/bin/opencortyx" init
  "$DIR/bin/opencortyx" up
fi

