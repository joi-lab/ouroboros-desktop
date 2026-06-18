# Ouroboros Desktop Host Operations (no Docker)

Installer is executed as root/sudo, but runtime runs under a dedicated Linux user (default: `ouroboros`).

Canonical runtime paths (for default user):

- `/home/ouroboros/.env` — config/secrets
- `/home/ouroboros/repo` — runtime repo
- `/home/ouroboros/data` — runtime state
- `/home/ouroboros/workspace` — files workspace root (accessible via dashboard)
- `/home/ouroboros/backups` — backup archives

Compatibility note:

- installer creates `~/Ouroboros -> ~/` symlink for legacy upstream fallback paths.

## Quick start

### Step 1a. Install latest `main`

```bash
curl -fsSL https://raw.githubusercontent.com/joi-lab/ouroboros-desktop/main/deploy/host/install.sh | sudo bash
```

### Step 1b. Install specific ref

```bash
REF="<tag-or-commit>"
curl -fsSL https://raw.githubusercontent.com/joi-lab/ouroboros-desktop/main/deploy/host/install.sh | sudo bash -s -- --ref "${REF}"
```

Optional runtime user override:

```bash
curl -fsSL https://raw.githubusercontent.com/joi-lab/ouroboros-desktop/main/deploy/host/install.sh | sudo bash -s -- --app-user myouro
```

### Step 2. Create SSH tunnel from operator machine

```bash
ssh -L 8765:127.0.0.1:8765 <user>@<host>
```

If runtime port differs from `8765`, forward that port instead.

### Step 3. Open dashboard and complete setup

- open `http://127.0.0.1:8765` in local browser (or your forwarded port)
- complete onboarding/setup wizard
- configure Telegram + provider API credentials in **Settings**

## Daily operations

```bash
sudo runuser -u ouroboros -- /home/ouroboros/repo/deploy/host/doctor.sh
sudo systemctl --machine=ouroboros@.host --user status ouroboros.service
sudo journalctl --user -M ouroboros@.host -u ouroboros.service -f
```

## Update and rollback

```bash
sudo runuser -u ouroboros -- /home/ouroboros/repo/deploy/host/update.sh --ref <tag-or-commit>
sudo runuser -u ouroboros -- /home/ouroboros/repo/deploy/host/update.sh --rollback
```

## Backup and restore

```bash
sudo runuser -u ouroboros -- /home/ouroboros/repo/deploy/host/backup.sh
sudo runuser -u ouroboros -- /home/ouroboros/repo/deploy/host/restore.sh /home/ouroboros/backups/ouroboros-desktop-<timestamp>.tar.gz
```

Hot backup (without stopping service):

```bash
sudo runuser -u ouroboros -- env STOP_SERVICE=0 /home/ouroboros/repo/deploy/host/backup.sh
```

## Browser tools profile

Default VPS deploy is lean (no Chromium tooling):

- `VPS_ENABLE_BROWSER_TOOLS=0` in `/home/ouroboros/.env`

Enable browser tooling only when needed for browser automation tasks:

```bash
sudo /home/ouroboros/repo/deploy/host/enable-browser-tools.sh
```

## Git ownership (simple model)

- Runtime repo is owned by the runtime user (`ouroboros`) at `/home/ouroboros/repo`.
- Installer/update scripts work with `seed` as upstream remote for platform updates.
- `origin` is free for your own fork/workflow.
- Run git operations for the runtime repo as the runtime user:

```bash
sudo runuser -u ouroboros -- git -C /home/ouroboros/repo status
```
