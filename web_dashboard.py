"""
Lightweight Web Dashboard and REST API for Twitch Drops Miner.
Built with aiohttp.web, completely decoupled from twitch.py internal logic.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from aiohttp import web

from constants import State

if TYPE_CHECKING:
    from twitch import Twitch

logger = logging.getLogger("TwitchDrops")

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Twitch Drops Miner - Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        twitch: {
                            purple: '#9146FF',
                            dark: '#0e0e10',
                            card: '#18181b',
                            border: '#27272a',
                            accent: '#772ce8'
                        }
                    }
                }
            }
        }
    </script>
</head>
<body class="bg-twitch-dark text-gray-200 font-sans min-h-screen flex flex-col">
    <!-- Header -->
    <header class="bg-twitch-card border-b border-twitch-border px-6 py-4 flex items-center justify-between sticky top-0 z-50">
        <div class="flex items-center space-x-3">
            <div class="w-8 h-8 rounded bg-twitch-purple flex items-center justify-center font-bold text-white shadow-lg shadow-twitch-purple/30">
                ⛏️
            </div>
            <div>
                <h1 class="text-lg font-bold text-white leading-tight">Twitch Drops Miner</h1>
                <p class="text-xs text-gray-400">Headless Docker Farm</p>
            </div>
        </div>
        <div class="flex items-center space-x-4">
            <span id="state-badge" class="px-3 py-1 rounded-full text-xs font-semibold bg-gray-800 text-gray-400 border border-gray-700">
                Connecting...
            </span>
            <span id="uptime-text" class="text-xs text-gray-400 hidden sm:inline">Uptime: --</span>
        </div>
    </header>

    <!-- Main Container -->
    <main class="flex-1 max-w-6xl w-full mx-auto p-4 sm:p-6 space-y-6">
        <!-- Live Status Card -->
        <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 sm:p-6 shadow-xl relative overflow-hidden">
            <div class="absolute top-0 left-0 w-2 h-full bg-twitch-purple"></div>
            
            <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-6">
                <!-- Stream & Channel Info -->
                <div class="space-y-3 flex-1">
                    <div class="flex items-center space-x-2">
                        <span class="text-xs font-semibold uppercase tracking-wider text-twitch-purple">Currently Watching</span>
                        <span id="pulse-dot" class="w-2 h-2 rounded-full bg-green-500 animate-ping"></span>
                    </div>

                    <div class="flex items-start sm:items-center space-x-4">
                        <div id="channel-avatar" class="w-14 h-14 rounded-full bg-gray-800 border-2 border-twitch-purple/50 flex items-center justify-center text-xl font-bold text-white overflow-hidden shadow">
                            📺
                        </div>
                        <div>
                            <h2 id="channel-name" class="text-2xl font-extrabold text-white leading-none">Scanning for streams...</h2>
                            <p id="game-name" class="text-sm text-gray-400 mt-1 font-medium">Waiting for active campaign</p>
                            <a id="stream-link" href="#" target="_blank" class="text-xs text-twitch-purple hover:underline mt-0.5 inline-block hidden">Open stream ↗</a>
                        </div>
                    </div>
                </div>

                <!-- Drop Progress Section -->
                <div class="flex-1 bg-twitch-dark/70 border border-twitch-border/70 rounded-xl p-4 space-y-3">
                    <div class="flex justify-between items-center text-sm">
                        <span id="drop-name" class="font-semibold text-white truncate max-w-[280px]">No active drop</span>
                        <span id="drop-percent" class="font-bold text-twitch-purple">0%</span>
                    </div>

                    <!-- Progress Bar -->
                    <div class="w-full bg-gray-800 h-3 rounded-full overflow-hidden border border-gray-700/50">
                        <div id="progress-bar" class="bg-gradient-to-r from-twitch-accent to-twitch-purple h-full rounded-full transition-all duration-500" style="width: 0%"></div>
                    </div>

                    <div class="flex justify-between text-xs text-gray-400">
                        <span id="drop-minutes">0 / 0 min</span>
                        <span id="campaign-drops-count">Drops: 0/0</span>
                    </div>
                </div>
            </div>

            <!-- Quick Action Buttons -->
            <div class="mt-6 pt-4 border-t border-twitch-border/60 flex flex-wrap gap-3">
                <button onclick="triggerAction('switch')" class="px-4 py-2 bg-twitch-purple/20 hover:bg-twitch-purple/30 text-twitch-purple border border-twitch-purple/40 rounded-lg text-xs font-semibold transition active:scale-95 flex items-center space-x-1.5">
                    <span>🔄</span>
                    <span>Switch Channel</span>
                </button>
                <button onclick="triggerAction('refresh')" class="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-200 border border-gray-700 rounded-lg text-xs font-semibold transition active:scale-95 flex items-center space-x-1.5">
                    <span>📦</span>
                    <span>Refresh Campaigns</span>
                </button>
                <button onclick="triggerAction('refresh-cookies')" class="px-4 py-2 bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/30 rounded-lg text-xs font-semibold transition active:scale-95 flex items-center space-x-1.5">
                    <span>🔐</span>
                    <span>Refresh Cookies (Playwright)</span>
                </button>
                <span id="action-msg" class="text-xs text-green-400 self-center hidden ml-2"></span>
            </div>
        </div>

        <!-- Metrics Grid -->
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <div class="bg-twitch-card border border-twitch-border rounded-xl p-4">
                <p class="text-xs text-gray-400 font-medium">Drops Claimed</p>
                <p id="stat-claimed" class="text-2xl font-black text-white mt-1">0</p>
            </div>
            <div class="bg-twitch-card border border-twitch-border rounded-xl p-4">
                <p class="text-xs text-gray-400 font-medium">Watch Time</p>
                <p id="stat-watch-time" class="text-2xl font-black text-white mt-1">0h</p>
            </div>
            <div class="bg-twitch-card border border-twitch-border rounded-xl p-4">
                <p class="text-xs text-gray-400 font-medium">Success Rate</p>
                <p id="stat-rate" class="text-2xl font-black text-green-400 mt-1">100%</p>
            </div>
            <div class="bg-twitch-card border border-twitch-border rounded-xl p-4">
                <p class="text-xs text-gray-400 font-medium">Priority Mode</p>
                <p id="stat-mode" class="text-2xl font-black text-twitch-purple mt-1 truncate">BALANCED</p>
            </div>
        </div>

        <!-- Active Campaigns Section -->
        <div class="bg-twitch-card border border-twitch-border rounded-xl p-5 space-y-4 shadow-lg">
            <div class="flex items-center justify-between border-b border-twitch-border pb-3">
                <h3 class="text-base font-bold text-white flex items-center space-x-2">
                    <span>🎯</span>
                    <span>Available Drops Campaigns</span>
                </h3>
                <span id="campaigns-count-badge" class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-gray-800 text-gray-300 border border-gray-700">0 campaigns</span>
            </div>

            <div id="campaigns-list" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                <div class="col-span-full py-8 text-center text-sm text-gray-500">Loading campaign data...</div>
            </div>
        </div>
    </main>

    <!-- Footer -->
    <footer class="text-center py-4 text-xs text-gray-500 border-t border-twitch-border/40 mt-auto">
        Twitch Drops Miner Headless &bull; Auto-refreshing every 3s
    </footer>

    <!-- Frontend Script -->
    <script>
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                if (!res.ok) return;
                const data = await res.json();
                updateUI(data);
            } catch (err) {
                console.error("Dashboard poll failed", err);
                document.getElementById('state-badge').innerText = 'Offline';
                document.getElementById('state-badge').className = 'px-3 py-1 rounded-full text-xs font-semibold bg-red-900/40 text-red-400 border border-red-800';
            }
        }

        function updateUI(data) {
            // Badge
            const badge = document.getElementById('state-badge');
            badge.innerText = data.state;
            if (data.state === 'IDLE') {
                badge.className = 'px-3 py-1 rounded-full text-xs font-semibold bg-gray-800 text-gray-300 border border-gray-700';
            } else if (data.state === 'EXIT') {
                badge.className = 'px-3 py-1 rounded-full text-xs font-semibold bg-red-900/40 text-red-400 border border-red-800';
            } else {
                badge.className = 'px-3 py-1 rounded-full text-xs font-semibold bg-green-900/40 text-green-300 border border-green-800';
            }

            // Channel & Game
            if (data.channel) {
                document.getElementById('channel-name').innerText = data.channel.name;
                document.getElementById('game-name').innerText = data.channel.game || 'No game specified';
                const link = document.getElementById('stream-link');
                link.href = data.channel.url;
                link.classList.remove('hidden');
                document.getElementById('pulse-dot').classList.remove('hidden');
            } else {
                document.getElementById('channel-name').innerText = 'Idle / Searching...';
                document.getElementById('game-name').innerText = 'No channel currently active';
                document.getElementById('stream-link').classList.add('hidden');
                document.getElementById('pulse-dot').classList.add('hidden');
            }

            // Drop Progress
            if (data.drop) {
                document.getElementById('drop-name').innerText = data.drop.name;
                document.getElementById('drop-percent').innerText = `${data.drop.progress_percent}%`;
                document.getElementById('progress-bar').style.width = `${data.drop.progress_percent}%`;
                document.getElementById('drop-minutes').innerText = `${data.drop.current_minutes} / ${data.drop.required_minutes} min`;
                document.getElementById('campaign-drops-count').innerText = `Campaign Drops: ${data.drop.campaign_claimed}/${data.drop.campaign_total}`;
            } else {
                document.getElementById('drop-name').innerText = 'No active drop';
                document.getElementById('drop-percent').innerText = '0%';
                document.getElementById('progress-bar').style.width = '0%';
                document.getElementById('drop-minutes').innerText = '0 / 0 min';
                document.getElementById('campaign-drops-count').innerText = 'Drops: 0/0';
            }

            // Metrics
            if (data.metrics) {
                document.getElementById('stat-claimed').innerText = data.metrics.drops_claimed || 0;
                document.getElementById('stat-watch-time').innerText = `${Math.round((data.metrics.total_minutes_watched || 0) / 60 * 10) / 10}h`;
                document.getElementById('stat-rate').innerText = `${Math.round(data.metrics.watch_success_rate || 100)}%`;
                document.getElementById('uptime-text').innerText = `Uptime: ${(data.metrics.uptime_hours || 0).toFixed(1)}h`;
            }
            if (data.priority_mode) {
                document.getElementById('stat-mode').innerText = data.priority_mode;
            }

            // Campaigns List
            const listContainer = document.getElementById('campaigns-list');
            if (data.campaigns && data.campaigns.length > 0) {
                document.getElementById('campaigns-count-badge').innerText = `${data.campaigns.length} campaigns`;
                listContainer.innerHTML = data.campaigns.map(c => `
                    <div class="bg-twitch-dark/60 border border-twitch-border rounded-lg p-3 hover:border-twitch-purple/50 transition">
                        <div class="flex items-center justify-between mb-1.5">
                            <span class="text-xs font-bold text-white truncate max-w-[180px]">${c.game}</span>
                            <span class="text-[10px] font-semibold px-2 py-0.5 rounded ${c.is_priority ? 'bg-twitch-purple/20 text-twitch-purple border border-twitch-purple/30' : 'bg-gray-800 text-gray-400'}">${c.is_priority ? 'Priority' : 'Normal'}</span>
                        </div>
                        <p class="text-xs text-gray-300 font-medium truncate mb-2">${c.name}</p>
                        <div class="w-full bg-gray-800 h-1.5 rounded-full overflow-hidden mb-1.5">
                            <div class="bg-twitch-purple h-full rounded-full" style="width: ${c.progress_percent}%"></div>
                        </div>
                        <div class="flex justify-between text-[11px] text-gray-400">
                            <span>Claimed: ${c.claimed}/${c.total}</span>
                            <span>${c.time_left}</span>
                        </div>
                    </div>
                `).join('');
            } else {
                document.getElementById('campaigns-count-badge').innerText = '0 campaigns';
                listContainer.innerHTML = '<div class="col-span-full py-6 text-center text-xs text-gray-500">No active campaigns in current inventory.</div>';
            }
        }

        async function triggerAction(action) {
            const msg = document.getElementById('action-msg');
            msg.innerText = 'Sending command...';
            msg.className = 'text-xs text-yellow-400 self-center ml-2';
            msg.classList.remove('hidden');

            try {
                const res = await fetch(`/api/action/${action}`, { method: 'POST' });
                const json = await res.json();
                msg.innerText = json.message || 'Action executed';
                msg.className = 'text-xs text-green-400 self-center ml-2';
                setTimeout(() => fetchStatus(), 500);
            } catch (err) {
                msg.innerText = 'Action failed';
                msg.className = 'text-xs text-red-400 self-center ml-2';
            }
            setTimeout(() => msg.classList.add('hidden'), 4000);
        }

        // Start polling every 3 seconds
        fetchStatus();
        setInterval(fetchStatus, 3000);
    </script>
</body>
</html>
"""


LOGIN_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Twitch Drops Miner</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-[#0e0e10] text-gray-200 font-sans min-h-screen flex items-center justify-center p-4">
    <div class="bg-[#18181b] border border-[#27272a] rounded-xl p-8 max-w-sm w-full shadow-2xl space-y-6">
        <div class="text-center space-y-2">
            <div class="w-12 h-12 rounded-xl bg-[#9146FF] mx-auto flex items-center justify-center text-2xl shadow-lg shadow-[#9146FF]/30">
                ⛏️
            </div>
            <h1 class="text-xl font-bold text-white">Twitch Drops Miner</h1>
            <p class="text-xs text-gray-400">Password protected dashboard</p>
        </div>
        <form method="POST" action="/login" class="space-y-4">
            <div>
                <label for="password" class="block text-xs font-semibold text-gray-400 mb-1 uppercase tracking-wider">Password</label>
                <input type="password" id="password" name="password" required autofocus class="w-full px-3.5 py-2.5 bg-[#0e0e10] border border-[#27272a] rounded-lg text-white text-sm focus:outline-none focus:border-[#9146FF] transition" placeholder="Enter dashboard password">
            </div>
            <button type="submit" class="w-full py-2.5 bg-[#9146FF] hover:bg-[#772ce8] text-white font-semibold rounded-lg text-sm transition shadow-lg shadow-[#9146FF]/20 active:scale-95">
                Unlock Dashboard
            </button>
        </form>
    </div>
</body>
</html>
"""


class WebDashboard:
    """Provides Web UI and REST API for Twitch Drops Miner."""

    def __init__(self, twitch: Twitch, host: str = "0.0.0.0", port: int = 8080):
        self.twitch = twitch
        self.host = host
        self.port = port
        self.password: str = getattr(twitch.settings, "web_password", "").strip()
        self._last_action_time: float = 0.0
        
        # Initialize aiohttp app with security middleware
        middlewares = [self._security_middleware]
        self.app = web.Application(middlewares=middlewares)
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._setup_routes()

    @web.middleware
    async def _security_middleware(self, request: web.Request, handler):
        # 1. Authentication check (if password is set)
        if self.password:
            # Allow POST /login
            if request.path == "/login" and request.method == "POST":
                return await handler(request)

            # Check header token, query param, or session cookie
            auth_header = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
            token_param = request.query.get("token", "").strip()
            cookie_token = request.cookies.get("tdm_auth", "").strip()

            is_authenticated = (
                auth_header == self.password
                or token_param == self.password
                or cookie_token == self.password
            )

            if not is_authenticated:
                if request.path.startswith("/api/"):
                    response = web.json_response({"error": "Unauthorized"}, status=401)
                else:
                    response = web.Response(text=LOGIN_HTML_TEMPLATE, content_type="text/html", status=401)
                self._apply_security_headers(response)
                return response

        # 2. Rate limit on actions
        if request.path.startswith("/api/action/"):
            now = asyncio.get_event_loop().time()
            if now - self._last_action_time < 1.0:
                response = web.json_response({"error": "Rate limited. Please wait 1 second between actions."}, status=429)
                self._apply_security_headers(response)
                return response
            self._last_action_time = now

        response = await handler(request)
        self._apply_security_headers(response)
        return response

    @staticmethod
    def _apply_security_headers(response: web.Response) -> None:
        """Apply defensive HTTP security headers to all responses."""
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    def _setup_routes(self):
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_post("/login", self.handle_login)
        self.app.router.add_get("/api/status", self.handle_status)
        self.app.router.add_post("/api/action/switch", self.handle_switch)
        self.app.router.add_post("/api/action/refresh", self.handle_refresh)
        self.app.router.add_post("/api/action/refresh-cookies", self.handle_refresh_cookies)

    async def handle_login(self, request: web.Request) -> web.Response:
        data = await request.post()
        submitted = data.get("password", "")
        if submitted == self.password:
            response = web.HTTPFound("/")
            response.set_cookie("tdm_auth", self.password, max_age=86400 * 30, httponly=True, samesite="Lax")
            return response
        return web.Response(text=LOGIN_HTML_TEMPLATE, content_type="text/html", status=401)

    async def handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=HTML_TEMPLATE, content_type="text/html")

    async def handle_status(self, request: web.Request) -> web.Response:
        """Returns JSON representation of the current miner status."""
        channel_data = None
        drop_data = None
        campaigns_data = []

        # 1. Channel Info
        watching_channel = self.twitch.watching_channel.get_with_default(None)
        if watching_channel is not None:
            channel_data = {
                "name": watching_channel.name,
                "game": watching_channel.game.name if watching_channel.game else None,
                "url": str(watching_channel.url),
            }

        # 2. Active Campaign and Drop
        active_campaign = self.twitch.get_active_campaign(watching_channel)
        if active_campaign is not None and active_campaign.active_drop is not None:
            drop = active_campaign.active_drop
            drop_data = {
                "name": drop.name,
                "rewards": drop.rewards_text(),
                "current_minutes": drop.current_minutes,
                "required_minutes": drop.required_minutes,
                "progress_percent": int(drop.progress * 100) if drop.progress else 0,
                "campaign_name": active_campaign.name,
                "campaign_claimed": active_campaign.claimed_drops,
                "campaign_total": active_campaign.total_drops,
            }

        # 3. Campaigns List
        now = datetime.now(timezone.utc)
        priority_games = set(self.twitch.settings.priority)
        for camp in sorted(self.twitch.inventory, key=lambda c: (c.game.name not in priority_games, c.ends_at)):
            hours_left = (camp.ends_at - now).total_seconds() / 3600
            time_left_str = f"{hours_left:.1f}h left" if hours_left > 0 else "Ending"
            campaigns_data.append({
                "game": camp.game.name,
                "name": camp.name,
                "claimed": camp.claimed_drops,
                "total": camp.total_drops,
                "progress_percent": int(camp.progress * 100),
                "is_priority": camp.game.name in priority_games,
                "time_left": time_left_str,
            })

        # 4. Metrics
        metrics_stats = {}
        if metrics := getattr(self.twitch, "metrics", None):
            metrics_stats = metrics.get_stats()

        data = {
            "state": self.twitch._state.name if hasattr(self.twitch, "_state") else "UNKNOWN",
            "priority_mode": self.twitch.settings.priority_mode.name,
            "channel": channel_data,
            "drop": drop_data,
            "campaigns": campaigns_data,
            "metrics": metrics_stats,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return web.json_response(data)

    async def handle_switch(self, request: web.Request) -> web.Response:
        """Forces channel switch."""
        logger.info("Web Dashboard: Force switch requested")
        self.twitch.stop_watching()
        self.twitch.change_state(State.CHANNEL_SWITCH)
        return web.json_response({"status": "ok", "message": "Channel switch triggered"})

    async def handle_refresh(self, request: web.Request) -> web.Response:
        """Forces campaigns inventory refresh."""
        logger.info("Web Dashboard: Campaigns refresh requested")
        self.twitch.change_state(State.INVENTORY_FETCH)
        return web.json_response({"status": "ok", "message": "Campaigns refresh triggered"})

    async def handle_refresh_cookies(self, request: web.Request) -> web.Response:
        """Triggers Playwright cookie refresh."""
        logger.info("Web Dashboard: Cookie refresh requested")
        try:
            from cookie_refresher import CookieRefresher
            refresher = CookieRefresher()
            success = await refresher.refresh_session(headless=True)
            if success:
                self.twitch.check_cookies_updated()
                return web.json_response({"status": "ok", "message": "Cookies refreshed successfully via Playwright!"})
            else:
                return web.json_response({"status": "error", "message": "Cookie refresh failed. Check browser profile."}, status=500)
        except Exception as e:
            logger.error(f"Web Dashboard cookie refresh error: {e}")
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def start(self):
        """Starts the aiohttp web server."""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        logger.info(f"Web Dashboard active at http://{self.host}:{self.port}")

    async def stop(self):
        """Stops the aiohttp web server gracefully."""
        if self.runner:
            await self.runner.cleanup()
            logger.info("Web Dashboard stopped")


_dashboard_instance: WebDashboard | None = None


async def start_web_dashboard(twitch: Twitch, host: str = "0.0.0.0", port: int = 8080) -> WebDashboard:
    """Global helper to start the dashboard."""
    global _dashboard_instance
    if _dashboard_instance is None:
        _dashboard_instance = WebDashboard(twitch, host=host, port=port)
        await _dashboard_instance.start()
    return _dashboard_instance


async def stop_web_dashboard():
    """Global helper to stop the dashboard."""
    global _dashboard_instance
    if _dashboard_instance is not None:
        await _dashboard_instance.stop()
        _dashboard_instance = None
