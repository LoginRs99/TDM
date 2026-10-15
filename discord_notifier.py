"""
Discord webhook notification system for Twitch Drops Miner
Enhanced with toggle disable/enable, rich readable embeds, and 1-week weekly summary digests.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp

from constants import State

if TYPE_CHECKING:
    from twitch import Twitch
    from inventory import TimedDrop

logger = logging.getLogger("TwitchDrops")


class DiscordNotifier:
    """Handles all Discord webhook notifications with smart batching, toggle control, and status monitoring"""

    def __init__(self, twitch: Twitch):
        self.twitch = twitch
        self._pending_drops: list[tuple[TimedDrop, datetime]] = []
        self._last_summary_sent: datetime = datetime.now(timezone.utc)
        self._last_weekly_summary_sent: datetime = datetime.now(timezone.utc)
        self._last_login_check: datetime = datetime.now(timezone.utc)
        self._login_status: bool = True
        self._notification_task: asyncio.Task | None = None
        self._last_login_alert: datetime | None = None
        self._login_alert_cooldown: timedelta = timedelta(hours=1)
        self._webhook_failures: int = 0
        self._max_webhook_failures: int = 5
        self._webhook_backoff_until: datetime | None = None
        self._session: aiohttp.ClientSession | None = None

    @property
    def is_enabled(self) -> bool:
        """Returns True if Discord webhook notifications are configured and enabled."""
        webhook_set = bool(self.twitch.settings.discord_webhook_url.strip())
        notifications_enabled = getattr(self.twitch.settings, "discord_notifications_enabled", True)
        return webhook_set and bool(notifications_enabled)

    def enable(self) -> bool:
        """Enable Discord webhook notifications and persist setting"""
        self.twitch.settings.discord_notifications_enabled = True
        self.twitch.settings.alter()
        self.twitch.settings.save()
        logger.info("Discord notifications have been ENABLED")
        return True

    def disable(self) -> bool:
        """Disable Discord webhook notifications and persist setting"""
        self.twitch.settings.discord_notifications_enabled = False
        self.twitch.settings.alter()
        self.twitch.settings.save()
        logger.info("Discord notifications have been DISABLED")
        return False

    def set_enabled(self, enabled: bool) -> bool:
        """Set Discord webhook notifications enabled state and persist setting"""
        self.twitch.settings.discord_notifications_enabled = bool(enabled)
        self.twitch.settings.alter()
        self.twitch.settings.save()
        logger.info(f"Discord notifications set to: {'ENABLED' if enabled else 'DISABLED'}")
        return self.twitch.settings.discord_notifications_enabled

    async def start(self):
        """Start the background notification task"""
        if self._notification_task is None or self._notification_task.done():
            self._notification_task = asyncio.create_task(self._notification_loop())
            logger.info("Discord notification system started")

    async def stop(self):
        """Stop and cleanup"""
        if self._notification_task:
            self._notification_task.cancel()
            try:
                await self._notification_task
            except asyncio.CancelledError:
                pass
        # Send final summary if there are pending drops
        if self._pending_drops and self.is_enabled:
            await self._send_summary()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return a persistent Discord webhook session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _notification_loop(self):
        """Background task that sends periodic summaries, weekly digest, and monitors login status"""
        while True:
            try:
                summary_interval = self.twitch.settings.discord_summary_interval_minutes
                await asyncio.sleep(60)  # Check every minute

                now = datetime.now(timezone.utc)

                # Check login status every 5 minutes
                if (now - self._last_login_check).total_seconds() >= 300:
                    await self._check_login_status()
                    self._last_login_check = now

                # 1-Week Notification / Weekly Digest check (every 7 days = 604800 seconds)
                time_since_weekly = (now - self._last_weekly_summary_sent).total_seconds()
                if time_since_weekly >= 7 * 86400:
                    if self.is_enabled:
                        await self.send_weekly_summary()
                    else:
                        self._last_weekly_summary_sent = now

                # Send summary if interval reached and we have drops
                if self._pending_drops and self.is_enabled:
                    time_since_last = (now - self._last_summary_sent).total_seconds() / 60
                    if time_since_last >= summary_interval:
                        await self._send_summary()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in notification loop: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _check_login_status(self):
        """Check if the Twitch login is still valid, and reload if cookies.jar was updated"""
        try:
            # Check if user placed a fresh cookies.jar file on disk
            if self.twitch.check_cookies_updated():
                logger.info("cookies.jar was updated on disk; re-validating login session...")
                try:
                    await self.twitch.get_auth()
                    self._login_status = True
                    self._last_login_alert = None
                    await self._send_login_alert(logged_out=False)
                    self.twitch.change_state(State.INVENTORY_FETCH)
                    return
                except Exception as e:
                    logger.warning(f"Re-authentication with new cookies.jar failed: {e}")

            auth_state = self.twitch._auth_state

            if not auth_state._hasattrs("access_token", "user_id"):
                if self._login_status:
                    self._login_status = False
                    await self._send_login_alert(logged_out=True)
                return

            try:
                async with self.twitch.request(
                    "GET",
                    "https://id.twitch.tv/oauth2/validate",
                    headers={"Authorization": f"OAuth {auth_state.access_token}"}
                ) as response:
                    if response.status == 401:
                        if self._login_status:
                            now = datetime.now(timezone.utc)
                            if (not self._last_login_alert or
                                now - self._last_login_alert > self._login_alert_cooldown):
                                self._login_status = False
                                self._last_login_alert = now
                                await self._send_login_alert(logged_out=True)
                    elif response.status == 200:
                        if not self._login_status:
                            self._login_status = True
                            self._last_login_alert = None
                            await self._send_login_alert(logged_out=False)
            except Exception as e:
                logger.debug(f"Login check request failed: {e}")

        except Exception as e:
            logger.error(f"Error checking login status: {e}", exc_info=True)

    async def _send_login_alert(self, logged_out: bool):
        """Send readable alert when login status changes"""
        if not self.is_enabled:
            return

        from version import __version__
        if logged_out:
            embed = {
                "title": "⚠️ Twitch Session Disconnected",
                "description": "**Your Twitch session has logged out or authentication cookies expired.**",
                "color": 15158332,  # Red (0xE74C3C)
                "fields": [
                    {
                        "name": "🔴 Current Impact",
                        "value": "The miner cannot advance drop timers or claim drops until re-authenticated.",
                        "inline": False,
                    },
                    {
                        "name": "🛠️ How to Restore",
                        "value": (
                            "1. Export fresh cookies with your browser extension in Netscape format\n"
                            "2. Save/replace `files/cookies.jar` (auto-reloads immediately)\n"
                            "3. Or use the `/2fa` command / interactive Playwright login"
                        ),
                        "inline": False,
                    },
                    {
                        "name": "⏰ Next Reminder",
                        "value": "A reminder alert will be sent in 1 hour if the session remains offline.",
                        "inline": False,
                    }
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": f"Twitch Drops Miner v{__version__} • Session Monitor"}
            }
        else:
            embed = {
                "title": "✅ Twitch Session Restored",
                "description": "**Successfully re-authenticated with Twitch. Drops mining has resumed!**",
                "color": 3066993,  # Green (0x2ECC71)
                "fields": [
                    {
                        "name": "🟢 Status",
                        "value": "OAuth tokens validated. Drop campaigns and streams are active.",
                        "inline": False,
                    }
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": f"Twitch Drops Miner v{__version__} • Session Monitor"}
            }

        await self._send_webhook({"embeds": [embed]})
        logger.info(f"Sent login alert: {'logged out' if logged_out else 'logged in'}")

    def add_drop(self, drop: TimedDrop):
        """Add a claimed drop to the pending summary"""
        self._pending_drops.append((drop, datetime.now(timezone.utc)))
        logger.info(f"Drop queued for summary: {drop.rewards_text()}")

    async def _send_summary(self):
        """Send a formatted, highly readable summary of all claimed drops since last summary"""
        if not self._pending_drops:
            return

        if not self.is_enabled:
            logger.debug("Skipping Discord summary; notifications are disabled.")
            return

        # Group drops by campaign
        campaigns_data: dict[str, dict] = {}
        for drop, claim_time in self._pending_drops:
            campaign_key = f"{drop.campaign.game.name}|{drop.campaign.name}"
            if campaign_key not in campaigns_data:
                campaigns_data[campaign_key] = {
                    "game": drop.campaign.game.name,
                    "campaign": drop.campaign.name,
                    "progress": f"{drop.campaign.claimed_drops}/{drop.campaign.total_drops}",
                    "drops": []
                }
            campaigns_data[campaign_key]["drops"].append((drop, claim_time))

        total_drops = len(self._pending_drops)
        time_range = self._get_time_range()

        # Build clean embed fields for maximum readability
        fields: list[dict] = [
            {"name": "📦 Claimed Drops", "value": f"**{total_drops}**", "inline": True},
            {"name": "🎯 Active Campaigns", "value": f"**{len(campaigns_data)}**", "inline": True},
            {"name": "⏱️ Time Range", "value": f"`{time_range}`", "inline": True},
        ]

        # Add up to 22 campaign fields (Discord embed limit is 25 fields total)
        sorted_campaigns = sorted(campaigns_data.values(), key=lambda x: x["game"])
        for campaign_data in sorted_campaigns[:22]:
            game_name = campaign_data["game"]
            campaign_name = campaign_data["campaign"]
            progress = campaign_data["progress"]
            drops = campaign_data["drops"]

            drop_lines = []
            for drop, claim_time in drops[:8]:
                time_str = claim_time.strftime("%H:%M UTC")
                drop_lines.append(f"• **{drop.rewards_text()}** (`{time_str}`)")

            if len(drops) > 8:
                drop_lines.append(f"*... and {len(drops) - 8} more drop{'s' if len(drops) - 8 != 1 else ''}*")

            field_val = f"📊 Progress: `{progress}` • Claimed: `{len(drops)}`\n" + "\n".join(drop_lines)
            if len(field_val) > 1024:
                field_val = field_val[:1020] + "..."

            fields.append({
                "name": f"🎮 {game_name} — {campaign_name}",
                "value": field_val,
                "inline": False,
            })

        if len(sorted_campaigns) > 22:
            remaining = len(sorted_campaigns) - 22
            fields.append({
                "name": "ℹ️ Additional Campaigns",
                "value": f"*... and {remaining} more campaign{'s' if remaining != 1 else ''} omitted for length.*",
                "inline": False,
            })

        from version import __version__
        embed = {
            "title": "🎁 Twitch Drops Mining Summary",
            "description": f"Successfully claimed **{total_drops}** drop{'s' if total_drops != 1 else ''} across **{len(campaigns_data)}** campaign{'s' if len(campaigns_data) != 1 else ''}.",
            "color": 9521151,  # Twitch Purple (0x9146FF)
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"Twitch Drops Miner v{__version__} • Summary Report"}
        }

        success = await self._send_webhook({"embeds": [embed]})
        if success:
            self._pending_drops.clear()
            self._last_summary_sent = datetime.now(timezone.utc)
            logger.info(f"Sent summary notification: {total_drops} drops across {len(campaigns_data)} campaigns")
        else:
            logger.warning("Failed to send summary, will retry next interval")

    def _get_time_range(self) -> str:
        """Helper to format time range"""
        if not self._pending_drops:
            return "No data"

        times = [t for _, t in self._pending_drops]
        first = min(times)
        last = max(times)

        if first.date() == last.date():
            return f"{first.strftime('%H:%M')} - {last.strftime('%H:%M UTC, %Y-%m-%d')}"
        return f"{first.strftime('%Y-%m-%d %H:%M')} - {last.strftime('%Y-%m-%d %H:%M UTC')}"

    async def send_weekly_summary(self, force: bool = False) -> bool:
        """
        Send a comprehensive 7-day weekly mining digest to Discord.
        Summarizes drops, watch hours, success rate, and uptime.
        """
        if not self.is_enabled and not force:
            logger.debug("Skipping weekly summary; notifications are disabled.")
            return False

        from version import __version__
        settings = self.twitch.settings
        metrics = getattr(self.twitch, "metrics", None)

        if metrics:
            stats = metrics.get_stats()
            drops_claimed = stats.get("drops_claimed", 0)
            drops_failed = stats.get("drops_failed", 0)
            drop_rate = stats.get("drop_success_rate", 0.0)
            watch_mins = stats.get("total_minutes_watched", 0)
            watch_hours = watch_mins / 60.0
            watch_rate = stats.get("watch_success_rate", 0.0)
            uptime_hours = stats.get("uptime_hours", 0.0)
            uptime_days = uptime_hours / 24.0
            streams_watched = stats.get("streams_watched", {})
        else:
            drops_claimed = len(self._pending_drops)
            drops_failed = 0
            drop_rate = 100.0 if drops_claimed > 0 else 0.0
            watch_hours = 0.0
            watch_mins = 0
            watch_rate = 100.0
            uptime_days = 0.0
            streams_watched = {}

        # Top channels/games watched
        if streams_watched:
            top_streams = sorted(streams_watched.items(), key=lambda x: x[1], reverse=True)[:5]
            stream_lines = [f"• **{ch}**: `{mins} mins` (~{mins/60:.1f}h)" for ch, mins in top_streams]
            watched_summary = "\n".join(stream_lines)
        else:
            watched_summary = "*No channel watch time recorded yet*"

        # Active campaigns count
        try:
            active_campaigns = len(getattr(self.twitch, "inventory", []))
        except Exception:
            active_campaigns = 0

        fields = [
            {
                "name": "🎁 7-Day Drops Claimed",
                "value": (
                    f"• **Total Claimed:** `{drops_claimed} drops`\n"
                    f"• **Failed Claims:** `{drops_failed}`\n"
                    f"• **Claim Success Rate:** `{drop_rate:.1f}%`"
                ),
                "inline": True,
            },
            {
                "name": "⏱️ Watch Time & Uptime",
                "value": (
                    f"• **Total Watched:** `{watch_hours:.1f} hrs`\n"
                    f"• **Watch Success:** `{watch_rate:.1f}%`\n"
                    f"• **Miner Uptime:** `{uptime_days:.1f} days`"
                ),
                "inline": True,
            },
            {
                "name": "📺 Top Stream Channels Watched",
                "value": watched_summary,
                "inline": False,
            },
            {
                "name": "🛡️ Miner & Session Status",
                "value": (
                    f"• **Twitch Auth:** `{'🟢 Valid' if self._login_status else '🔴 Expired'}`\n"
                    f"• **Priority Mode:** `{settings.priority_mode.name}`\n"
                    f"• **Priority Games:** `{len(settings.priority)} configured`\n"
                    f"• **Active Campaigns:** `{active_campaigns} in inventory`"
                ),
                "inline": False,
            },
        ]

        embed = {
            "title": "📅 Weekly Drops Mining Digest (7 Days)",
            "description": (
                "**Here is your weekly summary of drops mined, streaming activity, and performance over the past week.**"
            ),
            "color": 15844367,  # Gold (0xF1C40F)
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"Twitch Drops Miner v{__version__} • 1-Week Rollup"}
        }

        success = await self._send_webhook({"embeds": [embed]}, force=force)
        if success:
            self._last_weekly_summary_sent = datetime.now(timezone.utc)
            logger.info("Sent 1-week summary notification to Discord")
        return success

    async def send_test_notification(self) -> bool:
        """Send a test notification to verify webhook configuration (forces send even if toggle is off)"""
        webhook_url = self.twitch.settings.discord_webhook_url.strip()
        if not webhook_url:
            logger.info("Discord Webhook test failed: URL is not set in settings.")
            return False

        from version import __version__
        settings = self.twitch.settings
        interval = settings.discord_summary_interval_minutes
        enabled = getattr(settings, "discord_notifications_enabled", True)

        embed = {
            "title": "🔔 Discord Webhook Test Successful",
            "description": "**Your Twitch Drops Miner Discord notification channel is configured properly!**",
            "color": 3066993,  # Green (0x2ECC71)
            "fields": [
                {
                    "name": "📢 Notifications Toggle",
                    "value": f"**{'🟢 Enabled' if enabled else '⏸️ Disabled (Toggle is OFF)'}**",
                    "inline": True,
                },
                {
                    "name": "⏱️ Summary Interval",
                    "value": f"`{interval} minutes` (~{interval/60:.1f} hrs)",
                    "inline": True,
                },
                {
                    "name": "📅 1-Week Notification",
                    "value": "`Active (7-day digest)`",
                    "inline": True,
                },
                {
                    "name": "📋 Notification Features",
                    "value": (
                        "• **Rich Embed Summaries:** Clean visual breakdown of claimed drops\n"
                        "• **Weekly 7-Day Digest:** Comprehensive weekly mining rollup\n"
                        "• **Login State Alerts:** Instant alert if cookies expire\n"
                        "• **Toggle Control:** Can be enabled/disabled without removing webhook"
                    ),
                    "inline": False,
                }
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"Twitch Drops Miner v{__version__} • Webhook Test"}
        }

        success = await self._send_webhook({"embeds": [embed]}, force=True)
        if success:
            logger.info("Test notification sent successfully")
        else:
            logger.error("Test notification failed")
        return success

    async def send_startup_alert(self) -> bool:
        """Send a readable startup notification to Discord if configured and enabled"""
        if not self.is_enabled:
            return False

        from version import __version__
        settings = self.twitch.settings

        p_games = settings.priority
        if p_games:
            p_display = ", ".join(f"`{g}`" for g in p_games[:3])
            if len(p_games) > 3:
                p_display += f" *(+{len(p_games) - 3} more)*"
        else:
            p_display = "*None configured (All eligible games)*"

        ex_games = settings.exclude
        ex_count = len(ex_games)

        fields = [
            {
                "name": "⚙️ Mining Strategy",
                "value": f"• **Mode:** `{settings.priority_mode.name}`\n• **Summary Interval:** `{settings.discord_summary_interval_minutes}m`",
                "inline": True,
            },
            {
                "name": "🔔 Notifications",
                "value": f"• **Status:** `🟢 Enabled`\n• **Weekly Digest:** `Active`",
                "inline": True,
            },
            {
                "name": "🎯 Priority Games",
                "value": f"• **Configured ({len(p_games)}):** {p_display}\n• **Excluded Games:** `{ex_count}`",
                "inline": False,
            },
        ]

        features = []
        if getattr(settings, "auto_cookie_refresh", False):
            features.append("• **Auto Cookie Refresh:** `🟢 Enabled (Playwright)`")
        if getattr(settings, "web_dashboard", False):
            features.append(f"• **Web Dashboard:** `🟢 Active (Port {getattr(settings, 'web_port', 8080)})`")
        if getattr(settings, "discord_bot_token", "").strip():
            features.append("• **Interactive Discord Bot:** `🟢 Active (/status, /settings)`")

        if features:
            fields.append({
                "name": "🛡️ Extended Features",
                "value": "\n".join(features),
                "inline": False,
            })

        embed = {
            "title": "🚀 Twitch Drops Miner Started",
            "description": f"**Twitch Drops Miner v{__version__} is now active and farming drops.**",
            "color": 3447003,  # Blue (0x3498DB)
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"Twitch Drops Miner v{__version__} • Online"}
        }
        return await self._send_webhook({"embeds": [embed]})

    async def _send_webhook(self, payload: dict, max_retries: int = 3, force: bool = False) -> bool:
        """
        Internal method to send webhook with retry logic and error handling
        Returns True if successful, False otherwise
        """
        webhook_url = self.twitch.settings.discord_webhook_url.strip()
        if not webhook_url:
            return False

        if not force and not self.is_enabled:
            logger.debug("Discord webhook suppressed (notifications disabled)")
            return False

        now = datetime.now(timezone.utc)
        if self._webhook_backoff_until and now < self._webhook_backoff_until:
            logger.warning(
                f"Discord webhook is cooling down until "
                f"{self._webhook_backoff_until.isoformat()}"
            )
            return False
        if self._webhook_backoff_until and now >= self._webhook_backoff_until:
            logger.info("Retrying Discord webhook after cooldown")
            self._webhook_backoff_until = None

        for attempt in range(max_retries):
            try:
                session = await self._get_session()
                async with session.post(webhook_url, json=payload) as response:
                    # Success
                    if response.status in (200, 204):
                        if self._webhook_failures > 0:
                            logger.info(
                                f"Discord webhook recovered after "
                                f"{self._webhook_failures} failures"
                            )
                            self._webhook_failures = 0
                        self._webhook_backoff_until = None
                        logger.debug(f"Discord webhook sent successfully: {response.status}")
                        return True

                    # Rate limited
                    elif response.status == 429:
                        retry_after = int(response.headers.get('Retry-After', 5))
                        logger.warning(
                            f"Discord webhook rate limited. "
                            f"Retrying after {retry_after} seconds..."
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    # Client error
                    elif 400 <= response.status < 500:
                        response_text = await response.text()
                        logger.error(
                            f"Discord webhook client error {response.status}: "
                            f"{response_text[:200]}"
                        )
                        self._record_webhook_failure()
                        return False

                    # Server error
                    elif response.status >= 500:
                        logger.warning(
                            f"Discord webhook server error {response.status}. "
                            f"Retrying (attempt {attempt + 1}/{max_retries})..."
                        )
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)

                    else:
                        response_text = await response.text()
                        logger.error(
                            f"Discord webhook unexpected status {response.status}: "
                            f"{response_text[:200]}"
                        )
                        self._record_webhook_failure()
                        return False

            except asyncio.TimeoutError:
                logger.warning(
                    f"Discord webhook timeout (attempt {attempt + 1}/{max_retries})"
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

            except aiohttp.ClientError as e:
                logger.warning(
                    f"Discord webhook connection error: {e} "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

            except Exception as e:
                logger.error(
                    f"Unexpected error sending Discord webhook: {e}",
                    exc_info=True
                )
                self._record_webhook_failure()
                return False

        # All retries failed
        self._record_webhook_failure()
        logger.error(
            f"Discord webhook failed after {max_retries} attempts "
            f"(total failures: {self._webhook_failures})"
        )
        return False

    def _record_webhook_failure(self) -> None:
        """Record a webhook failure and schedule a retry window when needed."""
        self._webhook_failures += 1
        if self._webhook_failures >= self._max_webhook_failures:
            self._webhook_backoff_until = (
                datetime.now(timezone.utc) + timedelta(minutes=15)
            )
            logger.error(
                f"Discord webhook failed {self._webhook_failures} times. "
                f"Cooling down until {self._webhook_backoff_until.isoformat()}."
            )
