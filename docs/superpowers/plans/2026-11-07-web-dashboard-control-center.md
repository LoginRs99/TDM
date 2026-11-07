# Web Dashboard Control Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the Twitch Drops Miner web dashboard into a comprehensive, responsive full-page control center featuring four tabs (Overview, Campaigns, Settings, Live Logs) with end-to-end configuration management, campaign-to-priority fast actions, in-memory log streaming, and Twitch dark-theme styling.

**Architecture:** 
- Extend `web_dashboard.py` with REST endpoints (`/api/settings`, `/api/settings/priority`, `/api/settings/exclude`, `/api/logs`, `/api/notifications/test`, `/api/campaigns`).
- Implement an in-memory `WebLogHandler` (`logging.Handler`) using a circular buffer to capture and stream application logs to `/api/logs` without disk overhead.
- Overhaul the embedded frontend HTML/CSS/JS into a single-page application with 4 primary tabs (Overview, Campaigns, Settings, Live Logs), reactive state synchronization, toast notifications, and mobile-friendly responsive layouts.
- Validate and persist settings changes atomically to disk via `Settings.save(force=True)` while updating runtime components (logging level, Discord webhook intervals, and active channel queues) dynamically.

**Tech Stack:** Python 3.10+, `aiohttp.web`, Tailwind CSS (CDN), Vanilla ES6+ JavaScript, `logging.Handler`.

**Spec:** User requirements for a full-page tabbed control center (Overview, Campaigns, Settings, Live Logs) incorporating all settings, active campaign interactions, log streaming, and production-grade UI design.

## Global Constraints

- **Decoupled Architecture:** `web_dashboard.py` must interact with `twitch.py`, `settings.py`, and `discord_notifier.py` through public APIs without monkey-patching or altering core mining loops.
- **Operate Mode UX:** Restrained Twitch dark palette (`#0e0e10`, `#18181b`, `#27272a`, `#9146FF`), standard sans typography, 150-250ms transitions, instant action feedback toasts, zero layout shifts.
- **Security & Masking:** Sensitive credentials (webhook tokens, passwords) must be masked in `GET /api/settings`; endpoints must maintain CSRF/auth protection when `web_password` is configured.
- **Graceful Failure:** API errors must return structured JSON (`{"error": "message"}`) and be caught gracefully in the frontend with toast notifications.
- **Test Integrity:** All existing 23 tests must continue to pass, with new tests added to verify all new endpoints and behaviors.

## Review Focus

1. **Settings Persistence & Validation:** Invalid inputs (e.g., negative intervals, non-existent priority modes) must be rejected with 400 Bad Request and not corrupt `files/settings.json`.
2. **Dynamic Runtime Propagation:** Changing `logging_level` or `discord_summary_interval_minutes` in the UI must immediately affect the running logger and notification scheduler without restarting the process.
3. **Log Stream Memory Safety:** In-memory log buffer must be bounded (`maxlen=500`) to prevent memory leaks in 24/7 daemon runs.
4. **Fast Action State Sync:** Adding or removing a game from priority or exclude via campaign cards must immediately reflect across the UI and trigger channel reassessment.
5. **Mobile Responsiveness:** Tabs, grids, and settings controls must adapt cleanly to viewports down to 375px width without horizontal scrolling or overflowing text.

---

### Task 1: In-Memory Web Log Handler and Logs Endpoint

**Files:**
- Modify: `web_dashboard.py`
- Test: `test_suite.py`

- [ ] **Step 1.1: Write failing test for `WebLogHandler` and `/api/logs`**
  Add unit tests in `test_suite.py` testing:
  - `WebLogHandler` records logs into bounded buffer with timestamp, level, and message.
  - `GET /api/logs` returns log records with level filtering (`?level=WARNING`) and limit parameters.
  - `POST /api/logs/clear` empties the buffer.
- [ ] **Step 1.2: Run test suite to verify tests fail**
  `python test_suite.py`
- [ ] **Step 1.3: Implement `WebLogHandler` in `web_dashboard.py`**
  - Create `WebLogHandler(logging.Handler)` with `collections.deque(maxlen=500)`.
  - Format records with ISO-8601 UTC timestamp, level name, and formatted message.
  - Attach handler to `logging.getLogger("TwitchDrops")` upon dashboard startup and detach upon shutdown.
  - Implement `handle_logs(request)` and `handle_logs_clear(request)` routes in `WebDashboard`.
- [ ] **Step 1.4: Run test suite to verify tests pass**
  `python test_suite.py`
- [ ] **Step 1.5: Commit changes**
  `git commit -m "feat(dashboard): add in-memory log buffer and /api/logs endpoints"`

---

### Task 2: Settings REST API Endpoints & Validations

**Files:**
- Modify: `web_dashboard.py`
- Test: `test_suite.py`

- [ ] **Step 2.1: Write failing tests for `/api/settings` and sub-endpoints**
  Add unit tests in `test_suite.py` testing:
  - `GET /api/settings` returns current settings, priority modes, log levels, and known active games.
  - `POST /api/settings` validates and updates settings (`priority_mode`, intervals, toggles) and persists to disk.
  - `POST /api/settings/priority` supports `add`, `remove`, `move_up`, `move_down`, `top`.
  - `POST /api/settings/exclude` supports `add` and `remove`.
  - `POST /api/notifications/test` triggers test webhook dispatch.
- [ ] **Step 2.2: Run test suite to verify tests fail**
  `python test_suite.py`
- [ ] **Step 2.3: Implement settings handlers in `web_dashboard.py`**
  - Implement `handle_get_settings(request)`: serialize sanitized settings and option catalogs.
  - Implement `handle_update_settings(request)`: validate fields, apply to `self.twitch.settings`, apply runtime changes (log levels, discord notifications, channel switch if priority changed), and save.
  - Implement `handle_priority_action(request)`: handle ranking adjustments, additions, and removals.
  - Implement `handle_exclude_action(request)`: handle add and remove for exclusions.
  - Implement `handle_test_notification(request)`: dispatch test notification through `self.twitch.discord`.
- [ ] **Step 2.4: Run test suite to verify tests pass**
  `python test_suite.py`
- [ ] **Step 2.5: Commit changes**
  `git commit -m "feat(dashboard): add settings management, priority ordering, and test notification endpoints"`

---

### Task 3: Enhanced Campaigns and Account Status Endpoints

**Files:**
- Modify: `web_dashboard.py`
- Test: `test_suite.py`

- [ ] **Step 3.1: Write failing test for enhanced `/api/campaigns` and status data**
  Verify `/api/campaigns` returns list of campaigns with:
  - Game name, campaign title, time remaining, status.
  - Account link state (`is_linked`).
  - Drop details (name, reward text, required/current minutes, claimed status).
  - Flags for `is_priority` and `is_excluded`.
- [ ] **Step 3.2: Run test suite to verify tests fail**
  `python test_suite.py`
- [ ] **Step 3.3: Implement `handle_campaigns(request)` in `web_dashboard.py`**
  Extract full campaign drop details, link status (`campaign.linked`), and priority status from `self.twitch.inventory`.
- [ ] **Step 3.4: Run test suite to verify tests pass**
  `python test_suite.py`
- [ ] **Step 3.5: Commit changes**
  `git commit -m "feat(dashboard): add detailed /api/campaigns endpoint with drops breakdown"`

---

### Task 4: UI/UX Redesign — Tabbed Full-Page Control Center

**Files:**
- Modify: `web_dashboard.py` (HTML/CSS/JS template)
- Test: `test_suite.py`

- [ ] **Step 4.1: Construct Tab Navigation & Shell in `HTML_TEMPLATE`**
  - Header: Logo, Twitch API status badge, System RAM, Miner State, Uptime, Navigation Tabs (`Overview`, `Campaigns`, `Settings`, `Live Logs`), Logout button.
  - Floating Toast notification container.
- [ ] **Step 4.2: Build Overview Tab**
  - Hero Watching Card (channel name, avatar, live game, stream link, live ping dot).
  - Real-time Drop Progress (drop name, required minutes, progress bar with percentage, ETA, drops count).
  - Quick Action Buttons (Switch Channel, Refresh Campaigns, Playwright Cookie Refresh, Discord Notification Toggle).
  - 2FA Interactive Banner (screenshot + code input).
  - Stats Grid (Claimed, Watch Time, Success Rate, Priority Mode).
- [ ] **Step 4.3: Build Campaigns Tab**
  - Filter and search bar (search by game name, filter by Linked / Priority / Available).
  - Responsive campaign cards with reward breakdown, time remaining badge, and quick `+ Prioritize` / `🚫 Exclude` action buttons.
- [ ] **Step 4.4: Build Settings Tab**
  - **Strategy Card**: Priority Mode select, Connection Quality select, Stale Stream Timeout, Maintenance Interval, Toggles.
  - **Games Management Card**: Ranked Priority games list with drag/order controls (`▲`, `▼`, `⭐ Top`, `✖ Remove`), Add Game with autocomplete dropdown from active campaigns, Excluded games tag list with one-click remove.
  - **Discord Card**: Webhook URL input, Enable toggle, Summary interval presets (`1h`, `6h`, `12h`, `24h`, `1w`), "Send Test Webhook" button.
  - **Account & Security Card**: Auto-cookie refresh toggle, Web UI password change, "Log Out of Twitch" action.
  - Sticky/Inline "Save Settings" button with dirty state feedback.
- [ ] **Step 4.5: Build Live Logs Tab**
  - Dark monospace terminal view (`bg-[#09090b] text-gray-300`).
  - Level filters (`ALL`, `INFO`, `WARNING`, `ERROR`).
  - Auto-scroll toggle, "Copy Logs" button, and "Clear View" button.
  - Polling cycle streaming fresh records seamlessly.
- [ ] **Step 4.6: Implement Frontend Reactive JavaScript**
  - State management for active tab, settings dirty state, polling timers.
  - Toast feedback functions (`showToast(msg, type)`).
  - Fast action handlers (one-click prioritize/exclude from campaign cards with optimistic UI updates).
- [ ] **Step 4.7: Run test suite to verify template renders and passes all tests**
  `python test_suite.py`
- [ ] **Step 4.8: Commit changes**
  `git commit -m "feat(ui): implement tabbed full-page control center with settings, campaigns, and live logs"`

---

### Task 5: End-to-End Verification, Documentation & Git Push

**Files:**
- Modify: `FORK_GUIDE.md`
- Test: `test_suite.py`

- [ ] **Step 5.1: Run complete automated test suite**
  `python test_suite.py`
- [ ] **Step 5.2: Update `FORK_GUIDE.md`**
  Document the new tabbed control center architecture, new REST endpoints, and settings configuration flow.
- [ ] **Step 5.3: Commit and Push**
  - Commit all changes with descriptive commit message.
  - Push branch to `origin/custom/docker-headless-review`.
