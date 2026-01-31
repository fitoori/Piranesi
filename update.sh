#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
trap 'ec=$?; echo "ERROR: update failed (line $LINENO, exit $ec)" >&2; exit $ec' ERR

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_dir"

need(){ command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 not found" >&2; exit 127; }; }
need git
need docker

sudo_cmd=()
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    echo "ERROR: sudo not available; run as root to update packages." >&2
    exit 1
  fi
fi

echo "Stopping Docker Compose stack..."
docker compose down

echo "Updating system packages..."
"${sudo_cmd[@]}" apt-get update
"${sudo_cmd[@]}" apt-get -y upgrade
"${sudo_cmd[@]}" apt-get -y autoremove

if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: Working tree is dirty; commit or stash changes before syncing." >&2
  exit 1
fi

echo "Syncing repository..."
git pull --rebase

echo "Starting Docker Compose stack..."
docker compose up -d

echo "Update complete."
