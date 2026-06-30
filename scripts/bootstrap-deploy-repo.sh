#!/usr/bin/env bash
#
# Bootstrap the LOCAL-ONLY deployment repo for a test/live install.
#
# Creates a sibling `deploy/` git repo with NO remote — so it is physically
# impossible to push to GitHub — to version-control the things you DO want
# tracked on the VPS (real scenario yaml, systemd unit, ops runbook) while
# live customer data and secrets stay untracked.
#
# Run from the root of your hey-mr-postman clone on the VPS:
#     scripts/bootstrap-deploy-repo.sh
#
# Idempotent: safe to re-run; it won't clobber existing tracked files.
#
set -euo pipefail
GRN=$'\033[32m'; YEL=$'\033[33m'; NC=$'\033[0m'

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="${1:-$REPO_ROOT/deploy}"

mkdir -p "$DEPLOY"
cd "$DEPLOY"

if [ ! -d .git ]; then
  git init -q
  echo "${GRN}initialised${NC} local-only git repo at $DEPLOY"
else
  echo "${YEL}reusing${NC} existing git repo at $DEPLOY"
fi

# Hard guarantee: this repo must never have a remote.
if git remote | grep -q .; then
  echo "${YEL}WARNING:${NC} this deploy repo has a remote configured:"
  git remote -v | sed 's/^/    /'
  echo "  The deploy repo is meant to be LOCAL-ONLY. Remove it with:"
  echo "      git -C \"$DEPLOY\" remote remove <name>"
fi

# .gitignore — secrets + live data never tracked even here.
cat > .gitignore <<'EOF'
# Secrets and live data — never tracked, even in the local-only deploy repo.
.env
*.db
*.sqlite3
data/
kb/
attachments/
*.log
EOF

# Seed structure (only if absent — never overwrite real config).
[ -f scenario.yaml ]      || cp "$REPO_ROOT/config/scenarios/example.yaml" scenario.yaml
[ -f .env ]               || cp "$REPO_ROOT/.env.example" .env

if [ ! -f mail-agent.service ]; then
  cat > mail-agent.service <<'EOF'
[Unit]
Description=Hey Mr. Postman email agent (shadow mode)
After=network-online.target
Wants=network-online.target

[Service]
User=mailagent
WorkingDirectory=/home/mailagent/hey-mr-postman
EnvironmentFile=/home/mailagent/hey-mr-postman/deploy/.env
ExecStart=/home/mailagent/hey-mr-postman/.venv/bin/python -m scripts.run_agent run --scenario /home/mailagent/hey-mr-postman/deploy/scenario.yaml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
fi

if [ ! -f RUNBOOK.md ]; then
  cat > RUNBOOK.md <<'EOF'
# Deployment Runbook (local-only — DO NOT push)

- Scenario:  ./scenario.yaml   (real hosts/mailboxes — never leaves this box)
- Secrets:   ./.env            (gitignored even here)
- Service:   sudo cp mail-agent.service /etc/systemd/system/ && sudo systemctl daemon-reload

## Panic
- Stop sending: set KILL_SWITCH=1 in ./.env, then `sudo systemctl restart mail-agent`
- Stop agent:   sudo systemctl stop mail-agent

## Backup (off-box, encrypted only)
- tar + age:  tar czf - scenario.yaml RUNBOOK.md | age -p > deploy-backup.age
- NEVER back up data/ or .env in cleartext.
EOF
fi

# Install the same safety guards here.
"$REPO_ROOT/scripts/install-git-hooks.sh" "$DEPLOY" >/dev/null
echo "${GRN}installed${NC} pre-commit + pre-push guards in deploy repo"

cat <<EOF

${GRN}Deploy repo ready:${NC} $DEPLOY
  • no remote configured  → cannot push to GitHub
  • .env + data/ + kb/    → gitignored (and guarded)
  • edit scenario.yaml and .env with your real values, then:
        git -C "$DEPLOY" add -A && git -C "$DEPLOY" commit -m "deploy config"
  • do NOT run \`git remote add\` here. Back up via encrypted tarball only (see RUNBOOK.md).
EOF