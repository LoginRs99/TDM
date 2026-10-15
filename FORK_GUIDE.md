# Twitch Drops Miner — Fork Maintenance & Upstream Sync Guide

This guide documents the architecture of this headless/Docker-optimized fork, provides an inventory of custom vs. core modifications, and gives a step-by-step playbook for future agents and developers to sync upstream updates seamlessly without breaking custom features.

---

## 1. Fork Architecture & Philosophy

### Core vs. Headless Paradigm
The official upstream repository ([DevilXD/TwitchDropsMiner](https://github.com/DevilXD/TwitchDropsMiner)) is designed as a desktop application built around a Tkinter graphical user interface (`gui.py`) and system tray notifications.

This custom fork (`custom/docker-headless-review`) transforms the application into an autonomous, 24/7 headless Docker daemon suitable for servers, NAS devices, and cloud containers.

### The Decoupling Principle
To make future upstream merges as friction-free as possible:
1. **Minimize Core File Touches**: Core mining logic (`channel.py`, `inventory.py`, `twitch.py`, `websocket.py`, `utils.py`, `constants.py`) only receives changes strictly necessary for headless operation, race condition prevention, or upstream bug fixes.
2. **Encapsulate in Satellite Modules**: All extended functionality (Web Dashboard, Discord Bot, Cookie Refresher, Healthchecks, Metrics, Discord Webhook Notifier) resides in independent modules that plug into `Twitch` without modifying core mechanics.

---

## 2. Repository Topology & Branch Layout

| Branch | Purpose | Upstream Relationship |
| :--- | :--- | :--- |
| `master` | Clean mirror of official upstream | Direct 1:1 merge of `upstream/master`. Never add custom features here. |
| `custom/docker-headless-review` | Production headless branch | Contains headless engine, satellite modules, Docker containerization, and synced upstream fixes. |

> **Note on Branch Naming**: Git stores refs as files. If a branch named `custom/docker-headless-review` exists, Git disallows creating a branch literally named `custom` due to filesystem directory conflicts (`refs/heads/custom` is a folder). Always use full branch names.

---

## 3. Modification Inventory

### A. Core Files (Keep Minimal)

| File | What Was Changed | Why | Upstream Sync Precaution |
| :--- | :--- | :--- | :--- |
| `channel.py` | • Removed `self.display()` GUI calls.<br>• Added atomic `stream = self._stream` capture.<br>• Caught `RequestException` in `send_watch()`.<br>• Cached `_spade_url` with TTL as fallback. | Prevents GUI crashes, avoids `NoneType` race conditions when stream goes offline, and survives transient network drops. | When upstream updates `send_watch()` (e.g. HEAD playlist requests), preserve the local `stream = self._stream` assignment and `RequestException` try/except block. |
| `inventory.py` | • Replaced GUI notifications with `logger.info()`.<br>• Stubbed `_on_state_changed()` to `pass`.<br>• Synced zero-delta timer fix (`378697a`). | Headless mode has no Tkinter window/tray. | Ensure drop claim logging remains in `claim()` and delta bound checks in `_update_real_minutes()` / `update_minutes()` remain intact. |
| `constants.py` | • `LOG_PATH = Path(WORKING_DIR, "logs", "log.txt")`<br>• `PING_TIMEOUT = 30s`<br>• `WATCH_INTERVAL = 20s`<br>• Added `PriorityMode.BALANCED`. | Supports Docker volume mounts (`/logs`), prevents false timeout disconnects on high-latency hosts, and matches upstream HEAD request timing. | Upstream GQL query hashes should be updated whenever upstream refreshes them. Retain the `LOG_PATH` subdirectory and `PING_TIMEOUT` adjustment. |
| `websocket.py` | • Resilient exception handling on `ConnectionResetError` and `ClientConnectionError`.<br>• Auto-reconnect trigger on broken pipes. | Prevents unhandled websocket crashes in containerized network environments. | Keep error suppression and auto-reconnect triggers when merging upstream websocket changes. |
| `utils.py` | • Removed `tkinter` and `webbrowser` desktop imports.<br>• Hardened `json_save()` with atomic `.new` rename and Docker bind-mount `EBUSY` fallback.<br>• Added `is_special = is_special_events` alias.<br>• Maintained cross-platform file locking (`linux`, `darwin`, `win32`). | Prevents headless environments from attempting to load X11/Tk displays and allows settings persistence inside bind-mounted containers. | Do not re-introduce `tkinter` or GUI-dependent imports into `utils.py`. |
| `settings.py` | • Added Docker environment variable overrides (`DISCORD_*`, `WEB_*`, `TWITCH_*`).<br>• Added `discord_notifications_enabled: bool`.<br>• Added 1-week interval parsing (`1w`, `7d`, up to 10080 minutes). | Allows configuration via `.env` and `docker-compose.yml` without editing JSON files directly. | Keep environment variable loaders and notification toggle fields intact. |
| `main.py` | • Headless startup banner and configuration preflight validation.<br>• Added POSIX signal handling (`SIGINT`, `SIGTERM`) for clean Docker shutdowns.<br>• Spawns Web Dashboard and Discord Bot services in asyncio background tasks.<br>• Writes metrics on exit. | Replaces Tkinter main loop with server-grade daemon lifecycle. | Upstream changes to `main.py` are rare; when merging, preserve the async services initialization and signal handlers. |

### B. Satellite Modules (Custom & Fully Decoupled)

These files do not exist upstream. They can be edited, extended, or refactored freely without causing upstream merge conflicts:

1. **`discord_notifier.py`**:
   - Manages Discord webhook alerts (Startup, Claims, Logouts, Restorations, Summaries).
   - Features toggle function (`enable()`, `disable()`, `set_enabled()`, `is_enabled`).
   - Clean, readable embed structure with modular Discord Embed Fields.
   - Supports periodic summaries and full 7-day / 1-week digests (`send_weekly_summary()`).
2. **`discord_bot.py`**:
   - Full-featured Discord slash command bot (`/status`, `/channels`, `/claim`, `/restart`, `/reload`, `/inventory`, `/drops`, `/help`, `/weekly`).
   - `/notifications` command group (`status`, `enable`, `disable`, `interval`, `test`, `weekly`).
   - `/priority` command group with full ranking control:
     - `/priority list`: Shows ranked games with live mining and active drops status.
     - `/priority add <game> [rank]`: Add game or insert at a specific rank position.
     - `/priority move <game> <new_rank>`: Reorder an existing game's priority rank.
     - `/priority top <game>`: Promote any game directly to Rank #1 (highest priority).
     - `/priority remove <game>` & `/priority clear`: Delete entries or clear list.
   - Defers interactions to prevent Discord 3-second timeouts on slow operations.
3. **`web_dashboard.py`**:
   - Standalone `aiohttp` web server (default port `8080`).
   - Dynamic UI with channel switcher, manual claim, cookie refresher, and notification toggle button (`🔔 Discord Alerts: ON/OFF`).
   - Displays priority ranking badges (`#1 Priority`, etc.) and sorts campaigns by user rank order.
   - Exposes `/api/health` and `/api/metrics`.
4. **`cookie_refresher.py`**:
   - Automated Playwright session refresher & login solver.
   - Captures Twitch 2FA screenshots for remote solving (`/screenshots/2fa.png`).
   - Auto-converts Netscape `cookies.txt` or JSON formats into `cookies.jar`.
5. **`healthcheck.py` & `healthcheck_writer.py` & `healthcheck.sh`**:
   - Dual-layer container health monitoring (HTTP `/api/health` with dynamic `WEB_PORT` check + filesystem heartbeat age).
6. **`metrics.py`**:
   - Real-time and persisted runtime metrics (CPU, RAM, uptime, drop progress, claim counts).
7. **`config_validator.py`**:
   - Startup preflight checks for required files, file permissions, and cookies validity.
8. **`test_suite.py`**:
   - 18 automated tests covering upstream sync parity, zero-delta fixes, Discord alerts, interval parsing, Web UI toggle, healthcheck, Docker bind-mount EBUSY fallback, and priority ranking logic.

---

## 4. Step-by-Step Upstream Sync Playbook

Whenever upstream releases new commits (e.g. new Twitch GQL hashes, changes to mining intervals, or drop claiming adjustments), follow this exact workflow:

### Step 1: Fetch Upstream Changes
```bash
git fetch upstream
```

### Step 2: Update Local `master` Mirror
```bash
git checkout master
git merge upstream/master
# master is now an identical mirror of upstream
```

### Step 3: Inspect Upstream Changes
Check which commits were introduced upstream and what files they modified:
```bash
git log master -n 5 --stat
```
Identify whether upstream modified:
- **Core files** (`channel.py`, `inventory.py`, `constants.py`, `twitch.py`, `websocket.py`, `utils.py`).
- **GUI-only files** (`gui.py`, `translate.py`, `icons/`, `lang/`). **Ignore these entirely**.

### Step 4: Port Upstream Logic to `custom/docker-headless-review`
Switch to the custom branch:
```bash
git checkout custom/docker-headless-review
```

Depending on the nature of upstream commits:
- **Option A (Cherry-pick clean commits)**:
  If upstream commit only touches core logic without referencing Tkinter GUI:
  ```bash
  git cherry-pick <commit-hash>
  ```
- **Option B (Manual Port / Selective Merge)**:
  If upstream modified a core file (e.g. `channel.py` or `twitch.py`) that contains GUI calls in upstream:
  1. Inspect the diff: `git show <commit-hash> -- <filename>`
  2. Apply the core algorithmic logic to the file in `custom/docker-headless-review`.
  3. Ensure headless safeguards remain intact:
     - In `channel.py`: retain `stream = self._stream` and `RequestException` suppression.
     - In `inventory.py`: replace any new `gui` calls with `logger.info()`.
     - In `constants.py`: retain `LOG_PATH` subfolder and `PING_TIMEOUT = 30s`.
     - In `utils.py`: never re-add `import tkinter` or `import webbrowser`.

### Step 5: Run Automated Verification Tests
Always run the complete test suite before committing:
```bash
python test_suite.py
```
All tests must pass (`OK`). If a new upstream feature was ported, add a corresponding test case to `TestUpstreamSync` in `test_suite.py`.

### Step 6: Commit and Push
```bash
git add -u
git commit -m "sync(upstream): port <description of change> from <commit-hash>"
git push origin custom/docker-headless-review
```

---

## 5. Discord Notifications Reference

### Configuration & Controls
Notifications can be configured and toggled through 4 independent mechanisms:

1. **Environment Variable (`.env` / `docker-compose.yml`)**:
   ```env
   DISCORD_NOTIFICATIONS_ENABLED=true   # or false to silence all alerts
   DISCORD_SUMMARY_INTERVAL=1w          # 1w, 7d, 24h, 60m, or numeric minutes
   ```
2. **Settings JSON (`files/settings.json`)**:
   ```json
   {
       "discord_notifications_enabled": true,
       "discord_summary_interval_minutes": 10080
   }
   ```
3. **Discord Bot Slash Commands**:
   - `/notifications status`: View current notification state, webhook URL status, and interval.
   - `/notifications enable`: Turn webhook notifications ON.
   - `/notifications disable`: Turn webhook notifications OFF (mutes alerts while keeping URL configured).
   - `/notifications interval <val>`: Set summary interval (e.g. `1w`, `24h`, `60m`).
   - `/notifications test`: Fire a test embed to verify webhook connectivity (works even when disabled).
   - `/weekly`: Trigger an immediate 7-day digest.
4. **Web Dashboard**:
   - Click the **🔔 Discord Alerts: ON/OFF** button in the dashboard header for instant real-time toggle.

### Readability Standards
Embeds follow Discord's best-practice structured layout:
- **No Markdown Clutter**: Uses Discord Embed Fields (`inline=True` for compact side-by-side stats).
- **Clear Status Color-Coding**:
  - `0x9146FF` (Twitch Purple): Informational, startups, and tests.
  - `0x2ECC71` (Green): Successful drop claims and session restorations.
  - `0xE74C3C` (Red): Logouts, expired cookies, and critical errors.
  - `0x3498DB` (Blue): Periodic progress summaries and weekly digests.

---

## 6. Docker Integration & Image Publishing Workflow

### A. Automatic CI/CD Build & Push (`ghcr.io`)
The GitHub Actions workflow [.github/workflows/docker-image.yml](file:///.github/workflows/docker-image.yml) automatically builds multi-stage images and publishes them to GitHub Container Registry (`ghcr.io`):
- **Triggers**: Fires on pushes to `master`, `main`, and `custom/**`, semantic version tags (`v*`), or manual execution (`workflow_dispatch`).
- **Tagging**: Automatically tags images with `:latest` on `master` and `custom/docker-headless-review`, plus branch-specific tags (e.g. `:custom-docker-headless-review`, `:sha-<hash>`).
- **Permissions**: Requires repository Actions permissions set to **Read and write permissions** (Settings -> Actions -> General -> Workflow permissions).

### B. Building & Running Locally (No Registry Push Needed)
[docker-compose.yml](file:///docker-compose.yml) includes a direct `build` context:
```bash
# Build the container locally from source:
docker compose build

# Start the container locally (builds automatically if image is missing):
docker compose up -d

# Force rebuild local container after code edits:
docker compose up --build -d
```

### C. Manual Build & Push to GHCR on Your Own
To manually build and push the container image from your local terminal:
```bash
# 1. Log in to GitHub Container Registry using a GitHub Personal Access Token (PAT with write:packages scope):
echo $GITHUB_PAT | docker login ghcr.io -u <YOUR_GITHUB_USERNAME> --password-stdin

# 2. Build the Docker image locally:
docker build -t ghcr.io/<YOUR_GITHUB_USERNAME>/tdm:latest .

# 3. Push to GHCR:
docker push ghcr.io/<YOUR_GITHUB_USERNAME>/tdm:latest
```

---

## 7. Testing & Maintenance Checklist

Before releasing or deploying changes, run this checklist:

- [ ] Run `python test_suite.py` — verify 16/16 tests pass.
- [ ] Run module import audit:
  ```bash
  python -c "import channel, constants, inventory, utils, settings, twitch, main, web_dashboard, discord_notifier, discord_bot, cookie_refresher, metrics, healthcheck, healthcheck_writer, config_validator"
  ```
- [ ] Verify `healthcheck.py` responds to both HTTP check and file timestamp check.
- [ ] Ensure no desktop dependencies (`tkinter`, `PIL.ImageTk`, `webbrowser`) are imported by core modules.
- [ ] Verify `.env.example` contains any newly added configuration parameters.
- [ ] Verify Docker build passes: `docker compose build` or `docker build -t tdm:test .`
