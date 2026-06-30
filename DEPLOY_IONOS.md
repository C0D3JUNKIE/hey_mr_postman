# IONOS Deployment Plan — Hey Mr. Postman

> Implementation plan for running the agent on an **IONOS Cloud / VPS** server,
> using a GitHub **deploy key** as the deployment identity and a one-command
> update flow (`scripts/update.sh`) to pull releases from GitHub.
>
> This is the IONOS-specific *infrastructure* companion to
> [`TEST_PLAN.md`](TEST_PLAN.md) (which covers the agent walkthrough, the
> aggregation-mailbox wiring, and the shadow-mode test stages) and the
> local-only deploy-repo + commit guards from `scripts/`. Where steps overlap,
> this doc points there rather than repeating them.

---

## 0. What you'll end up with

```
  IONOS Cloud Panel
    └── Ubuntu 24.04 VPS  (firewall: inbound SSH only; outbound 443/993/465)
          ├── user: mailagent (unprivileged)
          ├── ~/hey-mr-postman/        ← git clone over SSH via DEPLOY KEY (read-only)
          │     └── .venv, scripts/update.sh
          ├── ~/hey-mr-postman/deploy/ ← local-only repo: real scenario.yaml + .env (no remote)
          └── systemd: mail-agent.service  (poll loop, shadow mode)

  GitHub repo  C0D3JUNKIE/hey_mr_postman
          └── Settings → Deploy keys → "ionos-prod" (READ-ONLY)  ← the deployment ID
```

**Why a deploy key is the "deployment ID":** it's an SSH keypair registered to
*this one repo*, **read-only** by default. The server can `git pull` with it but
can never push — which dovetails with the project rule that live data never goes
to GitHub. It's revocable per-repo (delete the key) and isn't tied to anyone's
personal GitHub account or a broad personal access token.

---

## 1. Provision the IONOS server

In the **IONOS Cloud Panel** (cloud.ionos.com) → *Compute Engine* (or *VPS*):

1. **Create server** → image **Ubuntu 24.04 LTS**.
2. **Size:** the local Chroma KB loads an ONNX embedding model, so give it
   headroom — **≥ 2 vCPU / 4 GB RAM / 40 GB SSD** is comfortable for v1
   (2 GB works but is tight once Chroma + the model are resident).
3. **SSH key:** upload your public key at creation (Cloud Panel → *SSH Keys*) so
   root login is key-based, not password.
4. **Region:** pick the datacenter nearest the cPanel mail hosts to keep IMAP/SMTP
   latency low.

### Firewall (IONOS Network → Firewall Policies)

The agent makes only **outbound** connections. Lock inbound down to SSH:

| Direction | Port | Why |
|-----------|------|-----|
| Inbound   | 22 (TCP) | SSH admin only — ideally restricted to your IP |
| Outbound  | 443 | Anthropic API + GitHub (HTTPS/SSH-over-443 optional) |
| Outbound  | 993 | IMAP read (hub mailbox) |
| Outbound  | 465 | SMTP send (per-brand identities) |
| Outbound  | 22  | GitHub SSH (git over SSH for pulls) |

No inbound mail/web ports are needed — mail arrives by **forwarding into the hub
mailbox**, which the agent *pulls* (see `TEST_PLAN.md` §4).

---

## 2. First login + service user

SSH in as root (IONOS emails the IP), then create an unprivileged service user
so the agent never runs as root:

```bash
ssh root@<server-ip>
apt update && apt -y upgrade
apt install -y python3 python3-venv python3-pip git
adduser --disabled-password --gecos "" mailagent
usermod -aG sudo mailagent          # only for the systemctl restart in update.sh
# (optional) copy your SSH key to the mailagent user for direct login:
rsync --archive --chown=mailagent:mailagent ~/.ssh /home/mailagent/
```

Work as `mailagent` from here on:

```bash
su - mailagent
```

---

## 3. GitHub deploy key — the deployment ID

Generate a keypair **on the server** (the private key never leaves the box):

```bash
ssh-keygen -t ed25519 -C "ionos-prod-hey-mr-postman" -f ~/.ssh/id_deploy -N ""
cat ~/.ssh/id_deploy.pub        # copy this PUBLIC key
```

Register the **public** key in GitHub:

1. Repo → **Settings → Deploy keys → Add deploy key**.
2. Title: `ionos-prod`. Paste the public key.
3. **Leave "Allow write access" UNCHECKED** (read-only — pulls only).

Tell SSH to use this key for GitHub (so `git` picks it up automatically):

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh -T git@github.com || true     # expect: "Hi C0D3JUNKIE/hey_mr_postman! ... does not provide shell access"
```

> **Rotation:** to revoke deployment access, delete the `ionos-prod` deploy key
> in GitHub and `rm ~/.ssh/id_deploy*` on the server. Generate a fresh pair to
> rotate. Because the key is read-only and per-repo, a compromised server cannot
> push to or reach any other repo.

---

## 4. Clone + install

Clone over **SSH** (uses the deploy key) so future pulls need no credentials:

```bash
cd ~
git clone git@github.com:C0D3JUNKIE/hey_mr_postman.git hey-mr-postman
cd hey-mr-postman

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # dev extras so update.sh can run the test gate
pytest -q                         # sanity: 41 passing

# install the commit/push safety guards into this clone (see TEST_PLAN.md §9)
scripts/install-git-hooks.sh .
```

---

## 5. Configuration (secrets + scenario)

Keep all real values in the **local-only deploy repo** (no GitHub remote — it
physically cannot leak), bootstrapped by the helper:

```bash
scripts/bootstrap-deploy-repo.sh          # creates ./deploy (git, no remote) + guards
```

Then fill in your real values in the gitignored files it seeds:

- `deploy/.env` — `ANTHROPIC_API_KEY`, `HUB_IMAP_PASSWORD`, per-brand SMTP
  passwords, `KILL_SWITCH=` (leave empty; set to `1` to force draft-only).
- `deploy/scenario.yaml` — real hub host/mailbox, `sending_identities`, brands.
  See `TEST_PLAN.md` §5 for each field and the brand-matching caveat.

Seed the KB:

```bash
mkdir -p kb/brand-a && python -m scripts.ingest_kb \
  --brand brand-a --scenario deploy/scenario.yaml
```

---

## 6. Run as a service (systemd)

The bootstrap wrote a unit template at `deploy/mail-agent.service` pointing at
`deploy/.env` + `deploy/scenario.yaml`. Install and start it:

```bash
sudo cp deploy/mail-agent.service /etc/systemd/system/mail-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now mail-agent
journalctl -u mail-agent -f           # watch the poll loop
```

The poll loop ingests mail, runs the SLA sweep each iteration, and runs the
retention/quota **housekeeping sweep once a day** (Phase 6). It ships in
`draft_only` — nothing auto-sends until you graduate it (`TEST_PLAN.md` §8).

---

## 7. Pull updates from GitHub — the easy path

A single command pulls the latest release and safely redeploys:

```bash
cd ~/hey-mr-postman
scripts/update.sh
```

`update.sh` is conservative by design:

1. **Refuses a dirty tree** — never discards local changes.
2. `git pull --ff-only` over the deploy key (fast-forward only; no merge surprises).
3. `pip install -e .` to pick up any dependency changes.
4. **Test gate:** runs `pytest -q`; if it fails, the service is **not** restarted.
5. `sudo systemctl restart mail-agent` only after green tests.

Flags: `--no-test` (skip the gate), `--service NAME` (non-default unit name).

> **Optional auto-update:** add a systemd timer or cron entry, but keep the test
> gate on so a bad release never silently restarts a broken agent:
> ```bash
> # crontab -e  (as mailagent) — check for updates nightly at 03:30
> 30 3 * * *  cd $HOME/hey-mr-postman && scripts/update.sh >> $HOME/update.log 2>&1
> ```

---

## 8. Optional: emailed daily digest

The poll loop logs the digest; to deliver it on a schedule, cron the command
(routing to email/Slack is a later seam — for now it prints to the log):

```bash
# crontab -e (as mailagent) — 08:00 daily digest into the journal/log
0 8 * * *  cd $HOME/hey-mr-postman && .venv/bin/python -m scripts.run_agent \
           digest --scenario deploy/scenario.yaml >> $HOME/digest.log 2>&1
```

---

## 9. Rollback / panic

- **Stop sending instantly:** set `KILL_SWITCH=1` in `deploy/.env`, then
  `sudo systemctl restart mail-agent` (forces `draft_only` regardless of config).
- **Stop the agent:** `sudo systemctl stop mail-agent`.
- **Roll back code:** `git -C ~/hey-mr-postman log --oneline` → `git checkout <sha>`
  → `scripts/update.sh --no-test` to reinstall+restart at that revision (or just
  `git pull` once the fix is on `main`).
- **Detach mail:** remove the cPanel forwarders feeding the hub (`TEST_PLAN.md` §4).
- Nothing is destructive: deletes are soft (`\Deleted` → `Trash/`), hard expunge
  only after `retention.trash_grace_days`.