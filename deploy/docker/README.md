# Ouroboros Headless VPS Operations

Canonical host paths:

- `/opt/ouroboros-headless` - repository and deploy scripts
- `/etc/ouroboros-headless/.env` - runtime config and secrets
- `/var/lib/ouroboros-headless` - persistent data
- `/var/backups/ouroboros-headless` - backup archives

## Quick start (VPS)

### Step 1a) Install latest `main`

```bash
curl -fsSL https://raw.githubusercontent.com/joi-lab/ouroboros-desktop/main/deploy/docker/install.sh | sudo bash
```

### Step 1b) Install a specific ref (reproducible tag/commit)

```bash
REF="<tag-or-commit>"
curl -fsSL "https://raw.githubusercontent.com/joi-lab/ouroboros-desktop/main/deploy/docker/install.sh" | sudo bash -s -- --ref "${REF}"
```

How to get a ref:

```bash
# latest tag
git ls-remote --tags --sort='v:refname' https://github.com/joi-lab/ouroboros-desktop.git | tail -n1

# latest main commit
git ls-remote https://github.com/joi-lab/ouroboros-desktop.git refs/heads/main
```

`--ref` is optional (`main` by default; prefer immutable tag/commit for production). `install.sh` is the single installer entrypoint; when run remotely (`curl | bash`) it self-fetches `common.sh` and runs interactive deploy setup for host-level values (`OUROBOROS_HOST_BIND`, port, file-browser root, browser-tools profile).

Installer resume/state metadata is stored in:

- `/opt/ouroboros-headless/.deploy/install-state.env`

Telegram/provider credentials are intentionally configured in Dashboard after first startup.

### Step 2) Create SSH tunnel from operator machine

```bash
ssh -L 8765:127.0.0.1:8765 <user>@<vps>
```

If runtime port differs from `8765`, forward that port instead.

### Step 3) Open dashboard and complete setup

- open `http://127.0.0.1:8765` in local browser (or your forwarded port)
- complete onboarding/setup wizard
- configure Telegram + provider API credentials in **Settings**

Non-interactive install (CI/automation):

```bash
curl -fsSL https://raw.githubusercontent.com/joi-lab/ouroboros-desktop/main/deploy/docker/install.sh | sudo bash -s -- --non-interactive
```

Install now also ensures:

- host packages: `curl`, `git`, `gh`, Docker Engine + Compose plugin
- container runtime binaries: `git`, `gh`, `curl`, `wget`, `jq`, `rg` (for in-app tooling and diagnostics)

## Browser tooling profile (VPS)

Default VPS build is lean (no Chromium tooling):

- `VPS_ENABLE_BROWSER_TOOLS=0` in `/etc/ouroboros-headless/.env`

Enable browser tooling only when needed for browser automation tasks:

```bash
sudo /opt/ouroboros-headless/deploy/docker/enable-browser-tools.sh
```

You can also force browser tooling in one-liner mode:

```bash
curl -fsSL https://raw.githubusercontent.com/joi-lab/ouroboros-desktop/main/deploy/docker/install.sh | sudo bash -s -- --ref <TAG> --with-browser
```

## Daily operations

```bash
sudo systemctl status ouroboros-headless
sudo journalctl -u ouroboros-headless -f
sudo /opt/ouroboros-headless/deploy/docker/doctor.sh
```

## Git remote ownership model (bind-mounted runtime repo)

The container now bind-mounts the host repository (`/opt/ouroboros-headless -> /app`).
Agent commits are persisted directly in the host git checkout.

Remote model:

- installer/upstream remote is stored as `seed` and used by `update.sh` for fetch/sync,
- runtime `origin` is intentionally separate and managed by app settings (`GITHUB_REPO` + `GITHUB_TOKEN`).

Practical behavior:

- **Updates**: `update.sh` syncs only local `main` from `seed` (`--ref`/`--rollback`).
- **Agent push/evolution**: goes to `origin` (auto-configured by the app when GitHub settings are set in UI).
- If UI GitHub settings are empty, `origin` may be absent and push is skipped/fails safely (no accidental push to upstream).

`update.sh` does not switch the active working branch to detached remote refs.

If `main` cannot fast-forward (or has uncommitted changes while checked out),
`update.sh` asks interactively whether to abort or hard-reset only `main`
(use `--force-main-reset` for non-interactive automation).

By design, `update.sh` runs `compose up -d` **without `--build`** in bind-mount mode.
This is fast for code-only updates, but image-level dependency changes (apt/system libs,
Python/Node packages installed at image build time, etc.) still require an explicit image rebuild.

`doctor.sh` checks `/api/health`, `/api/state` (`supervisor_ready`, `workers_alive`) and WebSocket handshake.

## Update and rollback

```bash
sudo /opt/ouroboros-headless/deploy/docker/update.sh --ref <tag-or-commit>
sudo /opt/ouroboros-headless/deploy/docker/update.sh --rollback
```

By default, updates require a clean git tree. Override only when necessary:

```bash
sudo /opt/ouroboros-headless/deploy/docker/update.sh --ref <ref> --allow-dirty
```

## Backup and restore

```bash
sudo /opt/ouroboros-headless/deploy/docker/backup.sh
sudo /opt/ouroboros-headless/deploy/docker/restore.sh /var/backups/ouroboros-headless/ouroboros-headless-<timestamp>.tar.gz
```

By default `backup.sh` stops the service for a consistent cold snapshot. To take a hot snapshot without stopping (useful for large data dirs or high-frequency cron jobs), set `STOP_SERVICE=0`:

```bash
sudo STOP_SERVICE=0 /opt/ouroboros-headless/deploy/docker/backup.sh
```

Hot snapshot is best-effort: in-flight writes may leave partial state. For production backups, cold mode (`STOP_SERVICE=1`, the default) is recommended.

## Validation checklist

- `curl -fsS http://127.0.0.1:<port>/api/health` returns 200
- `/api/state` reports `supervisor_ready=true` and `workers_alive>0`
- `/ws` handshake reports `101 Switching Protocols` in `doctor.sh`
- `systemctl is-active ouroboros-headless` returns `active`
- Telegram message/reply path works after reboot
- if `TELEGRAM_CHAT_ID` is unset: first inbound chat becomes active after restart
