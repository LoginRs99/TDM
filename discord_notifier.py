"""
Discord webhook notification system for Twitch Drops Miner
Enhanced with retry logic and better error handling
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from twitch import Twitch
    from inventory import TimedDrop

logger = logging.getLogger("TwitchDrops")


class DiscordNotifier:
    """Handles all Discord webhook notifications with smart batching and status monitoring"""
    
    def __init__(self, twitch: Twitch):
        self.twitch = twitch
        self._pending_drops: list[tuple[TimedDrop, datetime]] = []
        self._last_summary_sent: datetime = datetime.now(timezone.utc)
        self._last_login_check: datetime = datetime.now(timezone.utc)
        self._login_status: bool = True
        self._notification_task: asyncio.Task | None = None
        self._last_login_alert: datetime | None = None
        self._login_alert_cooldown: timedelta = timedelta(hours=1)
        self._webhook_failures: int = 0
        self._max_webhook_failures: int = 5
        self._webhook_backoff_until: datetime | None = None
        self._session: aiohttp.ClientSession | None = None
        
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
        if self._pending_drops:
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
        """Background task that sends periodic summaries and monitors login status"""
        while True:
            try:
                summary_interval = self.twitch.settings.discord_summary_interval_minutes
                await asyncio.sleep(60)  # Check every minute
                
                now = datetime.now(timezone.utc)
                
                # Check login status every 5 minutes
                if (now - self._last_login_check).total_seconds() >= 300:
                    await self._check_login_status()
                    self._last_login_check = now
                
                # Send summary if interval reached and we have drops
                if self._pending_drops:
                    time_since_last = (now - self._last_summary_sent).total_seconds() / 60
                    if time_since_last >= summary_interval:
                        await self._send_summary()
                        
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in notification loop: {e}", exc_info=True)
                await asyncio.sleep(60)
    
    async def _check_login_status(self):
        """Check if the Twitch login is still valid"""
        try:
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
        """Send alert when login status changes"""
        webhook_url = self.twitch.settings.discord_webhook_url.strip()
        if not webhook_url:
            return
        
        if logged_out:
            embed = {
                "title": "Twitch Login Session Lost",
                "description": (
                    "**Your Twitch session has been logged out!**\n\n"
                    "The miner cannot claim drops until you log back in.\n\n"
                    "**Action Required:**\n"
                    "- Replace the `cookies.jar` file with a fresh one\n"
                    "- Restart the container to re-authenticate\n\n"
                    "**Next alert will be sent in 1 hour if issue persists.**"
                ),
                "color": 15158332,  # Red
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Twitch Drops Miner - Login Monitor"}
            }
        else:
            embed = {
                "title": "Twitch Login Restored",
                "description": "Successfully logged back into Twitch. Drop mining has resumed.",
                "color": 3066993,  # Green
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "Twitch Drops Miner - Login Monitor"}
            }
        
        await self._send_webhook({"embeds": [embed]})
        logger.info(f"Sent login alert: {'logged out' if logged_out else 'logged in'}")
    
    def add_drop(self, drop: TimedDrop):
        """Add a claimed drop to the pending summary"""
        self._pending_drops.append((drop, datetime.now(timezone.utc)))
        logger.info(f"Drop queued for summary: {drop.rewards_text()}")
    
    async def _send_summary(self):
        """Send a summary of all claimed drops since last summary"""
        if not self._pending_drops:
            return
        
        webhook_url = self.twitch.settings.discord_webhook_url.strip()
        if not webhook_url:
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
        
        # Build description
        total_drops = len(self._pending_drops)
        time_range = self._get_time_range()
        
        description_parts = [
            f"**Summary Report** ({time_range})\n",
            f"**{total_drops} drop{'s' if total_drops != 1 else ''} claimed** across **{len(campaigns_data)} campaign{'s' if len(campaigns_data) != 1 else ''}**\n"
        ]
        
        # Add each campaign
        for campaign_data in sorted(campaigns_data.values(), key=lambda x: x["game"]):
            game_name = campaign_data["game"]
            campaign_name = campaign_data["campaign"]
            progress = campaign_data["progress"]
            drops = campaign_data["drops"]
            
            description_parts.append(
                f"\n**{game_name}** - {campaign_name}\n"
                f"Progress: {progress} | Claimed: {len(drops)} drop{'s' if len(drops) != 1 else ''}"
            )
            
            # List individual drops
            for drop, claim_time in drops:
                time_str = claim_time.strftime("%H:%M UTC")
                description_parts.append(f"   - {drop.rewards_text()} ({time_str})")
        
        embed = {
            "title": "Drops Mining Summary",
            "description": "\n".join(description_parts),
            "color": 5793266,  # Twitch Purple
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Twitch Drops Miner"}
        }
        
        success = await self._send_webhook({"embeds": [embed]})
        
        if success:
            # Clear pending drops and update timestamp only on success
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
        
    async def send_test_notification(self):
        """Send a test notification to verify webhook configuration"""
        webhook_url = self.twitch.settings.discord_webhook_url.strip()
        if not webhook_url:
            logger.info("Discord Webhook test failed: URL is not set in settings.")
            return False
        
        embed = {
            "title": "Webhook Test Successful",
            "description": (
                "Your Discord webhook is configured correctly.\n\n"
                "**Notification Settings:**\n"
                f"- Summary Interval: {self.twitch.settings.discord_summary_interval_minutes} minutes\n"
                f"- Login Monitoring: Enabled (checks every 5 minutes)\n\n"
                "You will receive:\n"
                "- Periodic summaries of claimed drops\n"
                "- Alerts when Twitch logs you out\n"
                "- Confirmation when login is restored"
            ),
            "color": 3066993,  # Green
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Twitch Drops Miner"}
        }
        
        success = await self._send_webhook({"embeds": [embed]})
        if success:
            logger.info("Test notification sent successfully")
        else:
            logger.error("Test notification failed")
        return success
    
    async def _send_webhook(self, payload: dict, max_retries: int = 3) -> bool:
        """
        Internal method to send webhook with retry logic and error handling
        Returns True if successful, False otherwise
        """
        webhook_url = self.twitch.settings.discord_webhook_url.strip()
        
        if not webhook_url:
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
                        # Reset failure counter on success
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

                    # Client error (bad request, unauthorized, etc)
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
                            await asyncio.sleep(2 ** attempt)  # Exponential backoff

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
