#!/usr/bin/env bash
#
# Pull the latest code from GitHub and redeploy — the VPS update helper.
#
# Safe to run repeatedly. Fast-forward only: it refuses to clobber local commits
# or a dirty tree, runs the test suite as a gate, and only then restarts the
# service. Designed to run as the unprivileged service user (uses sudo only for
# the systemctl restart).
#
# Usage:
#   scripts/update.sh                 # pull, test, restart mail-agent
#   scripts/update.sh --no-test       # skip the test gate (faster)
#   scripts/update.sh --service NAME  # restart a differently-named unit
#
set -euo pipefail
GRN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; NC=$'\033[0m'

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

SERVICE="mail-agent"
RUN_TESTS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-test) RUN_TESTS=0 ;;
    --service) shift; SERVICE="${1:?--service needs a name}" ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

# 1. Refuse to run on a dirty tree — never silently discard local changes.
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "${RED}working tree has uncommitted changes — aborting.${NC}"
  git status --short
  exit 1
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
before="$(git rev-parse --short HEAD)"
echo "${GRN}updating${NC} branch '$branch' (at $before) ..."

git fetch --prune origin
git pull --ff-only origin "$branch"
after="$(git rev-parse --short HEAD)"

if [ "$before" = "$after" ]; then
  echo "already up to date ($after); nothing to deploy."
  exit 0
fi
echo "advanced $before → ${GRN}$after${NC}"

# 2. Reinstall (picks up dependency changes in pyproject.toml).
if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
pip install -e . --quiet
echo "dependencies synced"

# 3. Test gate — do not restart a broken build.
if [ "$RUN_TESTS" -eq 1 ]; then
  if python -c "import pytest" >/dev/null 2>&1; then
    if ! pytest -q; then
      echo "${RED}tests failed — service NOT restarted. Investigate before retrying.${NC}"
      exit 1
    fi
  else
    echo "${YEL}pytest not installed (prod venv?) — skipping test gate.${NC}"
  fi
fi

# 4. Restart the service if it's installed.
if command -v systemctl >/dev/null 2>&1 && \
   systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE}\.service"; then
  sudo systemctl restart "$SERVICE"
  echo "${GRN}restarted${NC} ${SERVICE} — now at $after"
else
  echo "${YEL}service ${SERVICE} not installed; skipping restart.${NC}"
fi

echo "${GRN}update complete${NC} ($after)"