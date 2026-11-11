"""
Comprehensive test suite verifying:
1. Upstream sync (HEAD request mining, delta=0 drop timer fix, WATCH_INTERVAL)
2. Discord notification disable/enable functions
3. Discord notification readability (rich embed structure, fields, limits)
4. 1-week notification (settings parsing, weekly digest formatting, 7-day loop)
5. Discord bot notification commands
"""
import sys
import unittest
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import constants
from constants import PriorityMode, URLType
from settings import Settings, default_settings, SettingsFile
from inventory import TimedDrop
import discord_notifier
from discord_notifier import DiscordNotifier


class TestUpstreamSync(unittest.TestCase):
    def test_watch_interval(self):
        """Verify WATCH_INTERVAL was updated to 20 seconds from upstream f693b0c"""
        self.assertEqual(constants.WATCH_INTERVAL, timedelta(seconds=20))

    def test_inventory_zero_delta_fix(self):
        """Verify 378697a fix: delta == 0 does not return early and handles bound check"""
        from inventory import TimedDrop
        
        # Mock drop and campaign
        campaign_mock = MagicMock()
        drop = TimedDrop.__new__(TimedDrop)
        drop.campaign = campaign_mock
        drop.real_current_minutes = 10
        drop.required_minutes = 60
        drop.extra_current_minutes = 5
        drop._on_state_changed = MagicMock()

        # Test _update_real_minutes with delta == 0:
        # It should reset extra_current_minutes to 0 and call _on_state_changed()
        drop._update_real_minutes(0)
        self.assertEqual(drop.real_current_minutes, 10)
        self.assertEqual(drop.extra_current_minutes, 0)
        drop._on_state_changed.assert_called_once()

        # Test _update_real_minutes with negative bound
        drop._update_real_minutes(-20)
        self.assertEqual(drop.real_current_minutes, 0)

        # Test update_minutes with delta == 0
        campaign_mock.reset_mock()
        drop.update_minutes(0)  # new_minutes is 0, delta is 0 - 0 = 0
        campaign_mock._update_real_minutes.assert_called_with(0)

    def test_channel_send_watch_head_method(self):
        """Verify channel.send_watch uses stream playlist HEAD requests from f693b0c"""
        from channel import Channel
        import inspect

        # Inspect send_watch source/docstring
        doc = Channel.send_watch.__doc__ or ""
        self.assertIn("HEAD request on the stream's current playlist", doc)
        
        # Check that fallback _send_watch_spade exists
        self.assertTrue(hasattr(Channel, "_send_watch_spade"))

    def test_user_agent_update(self):
        """Verify Chrome 156 and Android app 31.8.0 user agents from upstream 69a4ff9"""
        from constants import ClientType
        self.assertIn("Chrome/156.0.0.0", str(ClientType.WEB.USER_AGENT))
        self.assertIn("tv.twitch.android.app/31.8.0/3108006", str(ClientType.ANDROID_APP.USER_AGENT))


class TestDiscordNotifications(unittest.TestCase):
    def setUp(self):
        # Create a mock twitch client with mock settings
        self.mock_settings = MagicMock()
        self.mock_settings.discord_webhook_url = "https://discord.com/api/webhooks/123/abc"
        self.mock_settings.discord_notifications_enabled = True
        self.mock_settings.discord_summary_interval_minutes = 60
        self.mock_settings.priority_mode = PriorityMode.BALANCED
        self.mock_settings.priority = ["Rust", "Sea of Thieves"]
        self.mock_settings.exclude = set()

        self.mock_twitch = MagicMock()
        self.mock_twitch.settings = self.mock_settings
        self.mock_twitch.metrics = MagicMock()
        self.mock_twitch.metrics.get_stats.return_value = {
            "drops_claimed": 12,
            "drops_failed": 0,
            "drop_success_rate": 100.0,
            "total_minutes_watched": 480,
            "watch_success_rate": 99.5,
            "uptime_hours": 168.0,
            "streams_watched": {"twitch_streamer_1": 300, "twitch_streamer_2": 180},
        }

        self.notifier = DiscordNotifier(self.mock_twitch)

    def test_is_enabled_property(self):
        """Verify is_enabled reflects both webhook URL presence and toggle state"""
        self.assertTrue(self.notifier.is_enabled)

        # Toggle off
        self.mock_settings.discord_notifications_enabled = False
        self.assertFalse(self.notifier.is_enabled)

        # Toggle on, but empty webhook URL
        self.mock_settings.discord_notifications_enabled = True
        self.mock_settings.discord_webhook_url = ""
        self.assertFalse(self.notifier.is_enabled)

    def test_disable_and_enable_functions(self):
        """Verify disable(), enable(), and set_enabled() update settings and save"""
        self.notifier.disable()
        self.assertFalse(self.mock_settings.discord_notifications_enabled)
        self.mock_settings.save.assert_called()

        self.notifier.enable()
        self.assertTrue(self.mock_settings.discord_notifications_enabled)
        self.mock_settings.save.assert_called()

        self.notifier.set_enabled(False)
        self.assertFalse(self.mock_settings.discord_notifications_enabled)

    def test_webhook_suppression_when_disabled(self):
        """Verify _send_webhook does not send requests when notifications are disabled unless forced"""
        self.notifier.disable()

        loop = asyncio.new_event_loop()
        try:
            # Without force: should return False immediately without making network calls
            result = loop.run_until_complete(
                self.notifier._send_webhook({"content": "test"}, force=False)
            )
            self.assertFalse(result)

            # With force=True: tries to send (we mock the session)
            mock_session = MagicMock()
            mock_session.closed = False
            mock_response = AsyncMock()
            mock_response.status = 204
            
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_response
            mock_ctx.__aexit__.return_value = None
            mock_session.post.return_value = mock_ctx
            self.notifier._session = mock_session

            result_forced = loop.run_until_complete(
                self.notifier._send_webhook({"content": "test"}, force=True)
            )
            self.assertTrue(result_forced)
        finally:
            loop.close()

    def test_summary_embed_readability(self):
        """Verify drops summary formatting uses rich structured embed fields"""
        # Create mock drop
        mock_drop = MagicMock()
        mock_drop.rewards_text.return_value = "Exclusive Weapon Skin"
        mock_drop.campaign.game.name = "Rust"
        mock_drop.campaign.name = "Rust Drops Round 29"
        mock_drop.campaign.claimed_drops = 3
        mock_drop.campaign.total_drops = 5

        self.notifier.add_drop(mock_drop)
        self.assertEqual(len(self.notifier._pending_drops), 1)

        # Capture payload sent to _send_webhook
        captured_payloads = []
        async def mock_send_webhook(payload, max_retries=3, force=False):
            captured_payloads.append(payload)
            return True

        self.notifier._send_webhook = mock_send_webhook

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.notifier._send_summary())
        finally:
            loop.close()

        self.assertEqual(len(captured_payloads), 1)
        embed = captured_payloads[0]["embeds"][0]

        # Verify title and color
        self.assertIn("🎁", embed["title"])
        self.assertEqual(embed["color"], 9521151)  # Twitch Purple

        # Verify fields structure for readability
        field_names = [f["name"] for f in embed["fields"]]
        self.assertTrue(any("Claimed Drops" in n for n in field_names))
        self.assertTrue(any("Active Campaigns" in n for n in field_names))
        self.assertTrue(any("Time Range" in n for n in field_names))
        self.assertTrue(any("Rust" in n for n in field_names))

        # Pending drops should be cleared on success
        self.assertEqual(len(self.notifier._pending_drops), 0)

    def test_weekly_summary_1_week_notification(self):
        """Verify 1-week weekly summary digest generates comprehensive 7-day stats"""
        captured_payloads = []
        async def mock_send_webhook(payload, max_retries=3, force=False):
            captured_payloads.append(payload)
            return True

        self.notifier._send_webhook = mock_send_webhook

        loop = asyncio.new_event_loop()
        try:
            ok = loop.run_until_complete(self.notifier.send_weekly_summary(force=True))
        finally:
            loop.close()

        self.assertTrue(ok)
        self.assertEqual(len(captured_payloads), 1)
        embed = captured_payloads[0]["embeds"][0]

        self.assertIn("7 Days", embed["title"])
        self.assertEqual(embed["color"], 15844367)  # Gold
        
        field_names = [f["name"] for f in embed["fields"]]
        self.assertTrue(any("7-Day Drops Claimed" in n for n in field_names))
        self.assertTrue(any("Watch Time & Uptime" in n for n in field_names))
        self.assertTrue(any("Top Stream Channels" in n for n in field_names))
        self.assertTrue(any("Miner & Session Status" in n for n in field_names))

    def test_startup_alert_formatting(self):
        """Verify startup alert embed has clean structure and fields"""
        captured_payloads = []
        async def mock_send_webhook(payload, max_retries=3, force=False):
            captured_payloads.append(payload)
            return True

        self.notifier._send_webhook = mock_send_webhook

        loop = asyncio.new_event_loop()
        try:
            ok = loop.run_until_complete(self.notifier.send_startup_alert())
        finally:
            loop.close()

        self.assertTrue(ok)
        embed = captured_payloads[0]["embeds"][0]
        self.assertIn("Twitch Drops Miner Started", embed["title"])
        self.assertEqual(embed["color"], 3447003)  # Blue
        field_names = [f["name"] for f in embed["fields"]]
        self.assertTrue(any("Mining Strategy" in n for n in field_names))
        self.assertTrue(any("Notifications" in n for n in field_names))
        self.assertTrue(any("Priority Games" in n for n in field_names))

    def test_login_alerts_formatting(self):
        """Verify login alert embeds (logged out and restored)"""
        captured_payloads = []
        async def mock_send_webhook(payload, max_retries=3, force=False):
            captured_payloads.append(payload)
            return True

        self.notifier._send_webhook = mock_send_webhook

        loop = asyncio.new_event_loop()
        try:
            # Test logged out
            loop.run_until_complete(self.notifier._send_login_alert(logged_out=True))
            self.assertEqual(len(captured_payloads), 1)
            embed_out = captured_payloads[0]["embeds"][0]
            self.assertIn("Disconnected", embed_out["title"])
            self.assertEqual(embed_out["color"], 15158332)  # Red

            # Test restored
            loop.run_until_complete(self.notifier._send_login_alert(logged_out=False))
            self.assertEqual(len(captured_payloads), 2)
            embed_in = captured_payloads[1]["embeds"][0]
            self.assertIn("Restored", embed_in["title"])
            self.assertEqual(embed_in["color"], 3066993)  # Green
        finally:
            loop.close()

    def test_test_notification_formatting(self):
        """Verify send_test_notification sends test embed with force=True"""
        captured_payloads = []
        captured_forces = []
        async def mock_send_webhook(payload, max_retries=3, force=False):
            captured_payloads.append(payload)
            captured_forces.append(force)
            return True

        self.notifier._send_webhook = mock_send_webhook

        loop = asyncio.new_event_loop()
        try:
            ok = loop.run_until_complete(self.notifier.send_test_notification())
        finally:
            loop.close()

        self.assertTrue(ok)
        self.assertTrue(captured_forces[0])
        embed = captured_payloads[0]["embeds"][0]
        self.assertIn("Test", embed["title"])
        self.assertEqual(embed["color"], 3066993)  # Green


class TestDiscordBotCommands(unittest.TestCase):
    def test_bot_commands_registration(self):
        """Verify /notifications and /weekly slash commands exist in DiscordBotService"""
        from discord_bot import DiscordBotService
        
        mock_settings = MagicMock()
        mock_settings.discord_bot_token = "fake_token"
        mock_twitch = MagicMock()
        mock_twitch.settings = mock_settings

        service = DiscordBotService(mock_twitch)
        cmd_names = [cmd.name for cmd in service.bot.tree.get_commands()]

        self.assertIn("notifications", cmd_names)
        self.assertIn("weekly", cmd_names)
        self.assertIn("status", cmd_names)
        self.assertIn("settings", cmd_names)

        # Check subcommands of /notifications
        notify_group = next(cmd for cmd in service.bot.tree.get_commands() if cmd.name == "notifications")
        sub_names = [c.name for c in notify_group.commands]
        self.assertIn("status", sub_names)
        self.assertIn("enable", sub_names)
        self.assertIn("disable", sub_names)
        self.assertIn("interval", sub_names)
        self.assertIn("test", sub_names)
        self.assertIn("weekly", sub_names)

        # Check subcommands of /priority
        prio_group = next(cmd for cmd in service.bot.tree.get_commands() if cmd.name == "priority")
        prio_sub_names = [c.name for c in prio_group.commands]
        self.assertIn("list", prio_sub_names)
        self.assertIn("add", prio_sub_names)
        self.assertIn("move", prio_sub_names)
        self.assertIn("top", prio_sub_names)
        self.assertIn("remove", prio_sub_names)
        self.assertIn("clear", prio_sub_names)

    def test_bot_resilience_and_views(self):
        """Verify Discord bot has persistent view, error handler, and sync guard"""
        from discord_bot import DiscordBotService, StatusView
        
        mock_settings = MagicMock()
        mock_settings.discord_bot_token = "fake_token"
        mock_twitch = MagicMock()
        mock_twitch.settings = mock_settings

        service = DiscordBotService(mock_twitch)
        self.assertFalse(service._synced)
        self.assertFalse(service._stopping)
        self.assertTrue(hasattr(service, "_bot_runner"))
        self.assertIsNotNone(service.bot.tree.on_error)
        
        # Verify persistent StatusView is registered in bot views
        has_status_view = any(isinstance(v, StatusView) for v in service.bot.persistent_views)
        self.assertTrue(has_status_view)


class DummyArgs:
    pass


class TestSettingsIntervals(unittest.TestCase):
    def test_settings_1_week_interval_parsing(self):
        """Verify DISCORD_SUMMARY_INTERVAL handles 1w, 7d, and up to 10080 minutes"""
        with patch.dict("os.environ", {"DISCORD_SUMMARY_INTERVAL": "1w"}):
            settings = Settings.__new__(Settings)
            settings._settings = dict(default_settings)
            settings._args = DummyArgs()
            settings._load_env_overrides()
            self.assertEqual(settings.discord_summary_interval_minutes, 10080)

        with patch.dict("os.environ", {"DISCORD_SUMMARY_INTERVAL": "7d"}):
            settings = Settings.__new__(Settings)
            settings._settings = dict(default_settings)
            settings._args = DummyArgs()
            settings._load_env_overrides()
            self.assertEqual(settings.discord_summary_interval_minutes, 10080)

        with patch.dict("os.environ", {"DISCORD_SUMMARY_INTERVAL": "10080"}):
            settings = Settings.__new__(Settings)
            settings._settings = dict(default_settings)
            settings._args = DummyArgs()
            settings._load_env_overrides()
            self.assertEqual(settings.discord_summary_interval_minutes, 10080)

    def test_settings_notifications_enabled_env(self):
        """Verify DISCORD_NOTIFICATIONS_ENABLED can be set to false/0/off"""
        for false_val in ("false", "0", "no", "off"):
            with patch.dict("os.environ", {"DISCORD_NOTIFICATIONS_ENABLED": false_val}):
                settings = Settings.__new__(Settings)
                settings._settings = dict(default_settings)
                settings._args = DummyArgs()
                settings._load_env_overrides()
                self.assertFalse(settings.discord_notifications_enabled)

        for true_val in ("true", "1", "yes", "on"):
            with patch.dict("os.environ", {"DISCORD_NOTIFICATIONS_ENABLED": true_val}):
                settings = Settings.__new__(Settings)
                settings._settings = dict(default_settings)
                settings._args = DummyArgs()
                settings._load_env_overrides()
                self.assertTrue(settings.discord_notifications_enabled)


class TestWebDashboardAndHealthcheck(unittest.TestCase):
    def test_dashboard_toggle_notifications(self):
        """Verify Web Dashboard toggle-notifications endpoint toggles notifier state"""
        from web_dashboard import WebDashboard
        
        mock_settings = MagicMock()
        mock_settings.discord_notifications_enabled = True
        mock_settings.discord_webhook_url = "https://discord.com/api/webhooks/1/abc"
        mock_settings.discord_summary_interval_minutes = 60
        mock_settings.web_password = ""

        mock_discord = MagicMock()
        def toggle_side_effect(val):
            mock_settings.discord_notifications_enabled = val
            return val
        mock_discord.set_enabled.side_effect = toggle_side_effect

        mock_twitch = MagicMock()
        mock_twitch.settings = mock_settings
        mock_twitch.discord = mock_discord

        dashboard = WebDashboard(mock_twitch, host="127.0.0.1", port=8080)
        
        loop = asyncio.new_event_loop()
        try:
            req = MagicMock()
            resp = loop.run_until_complete(dashboard.handle_toggle_notifications(req))
            import json
            data = json.loads(resp.text)
            self.assertEqual(data["status"], "ok")
            self.assertFalse(data["enabled"])
            mock_discord.set_enabled.assert_called_with(False)
        finally:
            loop.close()

    def test_healthcheck_port_env(self):
        """Verify healthcheck check_http_health reads WEB_PORT from environment"""
        from healthcheck import check_http_health
        with patch.dict("os.environ", {"WEB_PORT": "9090"}), \
             patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            ok, msg = check_http_health()
            self.assertTrue(ok)
            self.assertIn("9090", msg)

    def test_dashboard_status_network_field(self):
        """Verify Web Dashboard handle_status includes network health information"""
        from web_dashboard import WebDashboard
        import json

        mock_settings = MagicMock()
        mock_settings.priority_mode.name = "BALANCED"
        mock_settings.priority = []
        mock_settings.exclude = set()
        mock_settings.discord_notifications_enabled = True
        mock_settings.discord_webhook_url = ""
        mock_settings.discord_summary_interval_minutes = 60
        mock_settings.web_password = ""

        mock_twitch = MagicMock()
        mock_twitch._state.name = "IDLE"
        mock_twitch.settings = mock_settings
        mock_twitch.watching_channel.get_with_default.return_value = None
        mock_twitch.get_active_campaign.return_value = None
        mock_twitch.inventory = []
        mock_twitch.metrics.get_stats.return_value = {}
        mock_twitch.is_network_healthy = True
        mock_twitch._consecutive_gql_failures = 0

        dashboard = WebDashboard(mock_twitch, host="127.0.0.1", port=8080)
        loop = asyncio.new_event_loop()
        try:
            req = MagicMock()
            resp = loop.run_until_complete(dashboard.handle_status(req))
            data = json.loads(resp.text)
            self.assertIn("network", data)
            self.assertTrue(data["network"]["healthy"])
            self.assertEqual(data["network"]["consecutive_failures"], 0)
        finally:
            loop.close()

    def test_web_log_handler_and_api_logs(self):
        """Verify WebLogHandler captures logs and handle_logs returns filtered entries"""
        import logging
        from web_dashboard import WebDashboard, WebLogHandler
        import json

        handler = WebLogHandler(capacity=10)
        logger = logging.getLogger("TestWebLog")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        logger.info("Test info message")
        logger.warning("Test warning message")
        logger.error("Test error message")

        self.assertEqual(len(handler.buffer), 3)

        mock_twitch = MagicMock()
        mock_twitch.settings.web_password = ""
        dashboard = WebDashboard(mock_twitch, host="127.0.0.1", port=8080)
        dashboard.log_handler = handler

        loop = asyncio.new_event_loop()
        try:
            # All logs
            req_all = MagicMock()
            req_all.query = {}
            resp = loop.run_until_complete(dashboard.handle_logs(req_all))
            data = json.loads(resp.text)
            self.assertEqual(len(data["logs"]), 3)

            # Filter level WARNING (should include WARNING and ERROR)
            req_warn = MagicMock()
            req_warn.query = {"level": "WARNING"}
            resp_warn = loop.run_until_complete(dashboard.handle_logs(req_warn))
            data_warn = json.loads(resp_warn.text)
            self.assertEqual(len(data_warn["logs"]), 2)
            self.assertTrue(all(l["level"] in ("WARNING", "ERROR") for l in data_warn["logs"]))

            # Clear logs
            req_clear = MagicMock()
            resp_clear = loop.run_until_complete(dashboard.handle_logs_clear(req_clear))
            data_clear = json.loads(resp_clear.text)
            self.assertEqual(data_clear["status"], "ok")
            self.assertEqual(len(handler.buffer), 0)
        finally:
            loop.close()
            logger.removeHandler(handler)

    def test_dashboard_get_and_post_settings(self):
        """Verify GET /api/settings and POST /api/settings validation and updates"""
        from web_dashboard import WebDashboard
        import json

        mock_settings = MagicMock()
        mock_settings.priority_mode = PriorityMode.BALANCED
        mock_settings.priority = ["Rust"]
        mock_settings.exclude = {"Fortnite"}
        mock_settings.connection_quality = 1
        mock_settings.stale_stream_timeout_minutes = 5
        mock_settings.maintenance_interval_minutes = 20
        mock_settings.discord_webhook_url = "https://discord.com/api/webhooks/123/token_secret"
        mock_settings.discord_notifications_enabled = True
        mock_settings.discord_summary_interval_minutes = 60
        mock_settings.logging_level = "INFO"
        mock_settings.enable_badges_emotes = False
        mock_settings.available_drops_check = False
        mock_settings.auto_cookie_refresh = True
        mock_settings.web_port = 8080
        mock_settings.web_password = ""
        mock_settings.discord_bot_token = ""
        mock_settings.discord_owner_id = ""

        mock_twitch = MagicMock()
        mock_twitch.settings = mock_settings
        mock_twitch.inventory = []
        mock_twitch.discord = MagicMock()

        dashboard = WebDashboard(mock_twitch, host="127.0.0.1", port=8080)
        loop = asyncio.new_event_loop()
        try:
            # 1. GET /api/settings
            req_get = MagicMock()
            resp_get = loop.run_until_complete(dashboard.handle_get_settings(req_get))
            data_get = json.loads(resp_get.text)
            self.assertEqual(data_get["settings"]["priority_mode"], "BALANCED")
            self.assertIn("123", data_get["settings"]["discord_webhook_url"])
            self.assertNotIn("token_secret", data_get["settings"]["discord_webhook_url"])  # Masked!
            self.assertEqual(data_get["settings"]["priority"], ["Rust"])
            self.assertEqual(data_get["settings"]["exclude"], ["Fortnite"])
            self.assertIn("priority_modes", data_get["options"])

            # 2. POST /api/settings with valid updates
            req_post = MagicMock()
            req_post.json = AsyncMock(return_value={
                "priority_mode": 1,  # ENDING_SOONEST
                "connection_quality": 2,
                "stale_stream_timeout_minutes": 8,
                "maintenance_interval_minutes": 25,
                "discord_summary_interval_minutes": 120,
                "logging_level": "DEBUG",
                "enable_badges_emotes": True,
            })
            resp_post = loop.run_until_complete(dashboard.handle_update_settings(req_post))
            data_post = json.loads(resp_post.text)
            self.assertEqual(data_post["status"], "ok")
            mock_settings.save.assert_called()
            self.assertEqual(mock_settings.priority_mode, PriorityMode.ENDING_SOONEST)
            self.assertEqual(mock_settings.connection_quality, 2)
            self.assertEqual(mock_settings.stale_stream_timeout_minutes, 8)
            self.assertEqual(mock_settings.maintenance_interval_minutes, 25)
            self.assertEqual(mock_settings.discord_summary_interval_minutes, 120)
            self.assertEqual(mock_settings.logging_level, "DEBUG")
            self.assertTrue(mock_settings.enable_badges_emotes)

            # 3. POST /api/settings with invalid values (rejected)
            req_invalid = MagicMock()
            req_invalid.json = AsyncMock(return_value={
                "stale_stream_timeout_minutes": -5,
            })
            resp_invalid = loop.run_until_complete(dashboard.handle_update_settings(req_invalid))
            self.assertEqual(resp_invalid.status, 400)
        finally:
            loop.close()

    def test_dashboard_priority_and_exclude_actions(self):
        """Verify POST /api/settings/priority and POST /api/settings/exclude"""
        from web_dashboard import WebDashboard
        import json

        mock_settings = MagicMock()
        mock_settings.priority = ["Rust", "Apex Legends", "Valorant"]
        mock_settings.exclude = set(["Roblox"])
        mock_settings.web_password = ""

        mock_twitch = MagicMock()
        mock_twitch.settings = mock_settings

        dashboard = WebDashboard(mock_twitch, host="127.0.0.1", port=8080)
        loop = asyncio.new_event_loop()
        try:
            # Priority Add
            req_p_add = MagicMock()
            req_p_add.json = AsyncMock(return_value={"action": "add", "game": "Warframe"})
            resp = loop.run_until_complete(dashboard.handle_priority_action(req_p_add))
            data = json.loads(resp.text)
            self.assertEqual(data["status"], "ok")
            self.assertIn("Warframe", mock_settings.priority)

            # Priority Move Up
            req_p_up = MagicMock()
            req_p_up.json = AsyncMock(return_value={"action": "move_up", "game": "Apex Legends"})
            resp_up = loop.run_until_complete(dashboard.handle_priority_action(req_p_up))
            self.assertEqual(json.loads(resp_up.text)["priority"][0], "Apex Legends")

            # Priority Top
            req_p_top = MagicMock()
            req_p_top.json = AsyncMock(return_value={"action": "top", "game": "Valorant"})
            resp_top = loop.run_until_complete(dashboard.handle_priority_action(req_p_top))
            self.assertEqual(json.loads(resp_top.text)["priority"][0], "Valorant")

            # Exclude Add
            req_e_add = MagicMock()
            req_e_add.json = AsyncMock(return_value={"action": "add", "game": "FIFA 23"})
            resp_e_add = loop.run_until_complete(dashboard.handle_exclude_action(req_e_add))
            self.assertIn("FIFA 23", mock_settings.exclude)

            # Exclude Remove
            req_e_rem = MagicMock()
            req_e_rem.json = AsyncMock(return_value={"action": "remove", "game": "Roblox"})
            resp_e_rem = loop.run_until_complete(dashboard.handle_exclude_action(req_e_rem))
            self.assertNotIn("Roblox", mock_settings.exclude)
        finally:
            loop.close()

    def test_dashboard_test_notification(self):
        """Verify POST /api/notifications/test triggers discord test notification"""
        from web_dashboard import WebDashboard
        import json

        mock_settings = MagicMock()
        mock_settings.discord_webhook_url = "https://discord.com/api/webhooks/1/abc"
        mock_settings.web_password = ""

        mock_discord = MagicMock()
        mock_discord.send_test_notification = AsyncMock(return_value=True)

        mock_twitch = MagicMock()
        mock_twitch.settings = mock_settings
        mock_twitch.discord = mock_discord

        dashboard = WebDashboard(mock_twitch, host="127.0.0.1", port=8080)
        loop = asyncio.new_event_loop()
        try:
            req = MagicMock()
            resp = loop.run_until_complete(dashboard.handle_test_notification(req))
            data = json.loads(resp.text)
            self.assertEqual(data["status"], "ok")
            mock_discord.send_test_notification.assert_awaited_once()
        finally:
            loop.close()

    def test_dashboard_campaigns_endpoint(self):
        """Verify GET /api/campaigns returns structured campaigns with drops breakdown"""
        from web_dashboard import WebDashboard
        import json

        mock_drop = MagicMock()
        mock_drop.id = "d1"
        mock_drop.name = "AK Skin"
        mock_drop.required_minutes = 120
        mock_drop.current_minutes = 60
        mock_drop.is_claimed = False
        mock_drop.progress = 0.5

        mock_camp = MagicMock()
        mock_camp.id = "c1"
        mock_camp.game.name = "Rust"
        mock_camp.name = "Rust Drops"
        mock_camp.status = "ACTIVE"
        mock_camp.linked = True
        mock_camp.starts_at = datetime.now(timezone.utc) - timedelta(hours=2)
        mock_camp.ends_at = datetime.now(timezone.utc) + timedelta(hours=24)
        mock_camp.claimed_drops = 0
        mock_camp.total_drops = 1
        mock_camp.progress = 0.5
        mock_camp.timed_drops = {mock_drop.id: mock_drop}

        mock_settings = MagicMock()
        mock_settings.priority = ["Rust"]
        mock_settings.exclude = set()
        mock_settings.web_password = ""

        mock_twitch = MagicMock()
        mock_twitch.settings = mock_settings
        mock_twitch.inventory = [mock_camp]

        dashboard = WebDashboard(mock_twitch, host="127.0.0.1", port=8080)
        loop = asyncio.new_event_loop()
        try:
            req = MagicMock()
            resp = loop.run_until_complete(dashboard.handle_campaigns(req))
            data = json.loads(resp.text)
            self.assertEqual(data["status"], "ok")
            self.assertEqual(len(data["campaigns"]), 1)
            camp_info = data["campaigns"][0]
            self.assertEqual(camp_info["game"], "Rust")
            self.assertTrue(camp_info["is_linked"])
            self.assertTrue(camp_info["is_priority"])
            self.assertFalse(camp_info["is_excluded"])
            self.assertEqual(len(camp_info["drops"]), 1)
            self.assertEqual(camp_info["drops"][0]["name"], "AK Skin")
        finally:
            loop.close()

    def test_pause_and_resume_mining_and_version(self):
        """Verify pause/resume mining mechanics, API endpoints, state transition, and versioning"""
        from web_dashboard import WebDashboard
        from twitch import Twitch
        from constants import State
        import json

        # 1. Test Twitch class pause/resume/toggle_pause methods directly
        twitch_mock = MagicMock(spec=Twitch)
        twitch_mock.is_paused = False
        twitch_mock._state = State.CHANNEL_SWITCH
        twitch_mock._state_change = MagicMock()
        twitch_mock.watching_channel = MagicMock()
        twitch_mock.watching_channel.get_with_default.return_value = None
        twitch_mock.inventory = []
        twitch_mock.settings = MagicMock()
        twitch_mock.settings.priority = []
        twitch_mock.settings.exclude = set()
        twitch_mock.settings.priority_mode = MagicMock()
        twitch_mock.settings.priority_mode.name = "BALANCED"
        twitch_mock.settings.web_password = ""

        twitch_mock.change_state = lambda s: setattr(twitch_mock, "_state", s)
        twitch_mock.stop_watching = MagicMock()
        twitch_mock.pause = lambda: Twitch.pause(twitch_mock)
        twitch_mock.resume = lambda: Twitch.resume(twitch_mock)
        twitch_mock.toggle_pause = lambda: Twitch.toggle_pause(twitch_mock)

        # Initially active
        self.assertFalse(twitch_mock.is_paused)
        self.assertEqual(twitch_mock._state, State.CHANNEL_SWITCH)

        # Trigger Pause
        res_toggled = twitch_mock.toggle_pause()
        self.assertTrue(res_toggled)
        self.assertTrue(twitch_mock.is_paused)
        self.assertEqual(twitch_mock._state, State.IDLE)
        twitch_mock.stop_watching.assert_called_once()

        # Trigger Resume
        res_toggled_back = twitch_mock.toggle_pause()
        self.assertFalse(res_toggled_back)
        self.assertFalse(twitch_mock.is_paused)
        self.assertEqual(twitch_mock._state, State.CHANNEL_SWITCH)

        # 2. Test WebDashboard pause / resume / toggle endpoints and versioning
        dashboard = WebDashboard(twitch_mock, host="127.0.0.1", port=8080)
        loop = asyncio.new_event_loop()
        try:
            # POST /api/action/pause
            req_pause = MagicMock()
            resp_pause = loop.run_until_complete(dashboard.handle_pause(req_pause))
            data_pause = json.loads(resp_pause.text)
            self.assertEqual(data_pause["status"], "ok")
            self.assertTrue(data_pause["is_paused"])
            self.assertEqual(data_pause["state"], "PAUSED")
            self.assertTrue(dashboard.is_paused)

            # GET /api/status while paused
            req_status = MagicMock()
            resp_status = loop.run_until_complete(dashboard.handle_status(req_status))
            data_status = json.loads(resp_status.text)
            self.assertEqual(data_status["version"], "v2.5-headless")
            self.assertTrue(data_status["is_paused"])
            self.assertEqual(data_status["state"], "PAUSED")

            # GET /api/health while paused
            req_health = MagicMock()
            resp_health = loop.run_until_complete(dashboard.handle_health(req_health))
            data_health = json.loads(resp_health.text)
            self.assertEqual(data_health["version"], "v2.5-headless")
            self.assertTrue(data_health["is_paused"])
            self.assertEqual(data_health["state"], "PAUSED")

            # POST /api/action/resume
            req_resume = MagicMock()
            resp_resume = loop.run_until_complete(dashboard.handle_resume(req_resume))
            data_resume = json.loads(resp_resume.text)
            self.assertEqual(data_resume["status"], "ok")
            self.assertFalse(data_resume["is_paused"])
            self.assertEqual(data_resume["state"], "CHANNEL_SWITCH")
            self.assertFalse(dashboard.is_paused)

            # POST /api/action/toggle-pause (from unpaused -> paused)
            req_toggle = MagicMock()
            resp_toggle = loop.run_until_complete(dashboard.handle_toggle_pause(req_toggle))
            data_toggle = json.loads(resp_toggle.text)
            self.assertTrue(data_toggle["is_paused"])
            self.assertTrue(dashboard.is_paused)

            # POST /api/action/switch (should unpause automatically)
            req_switch = MagicMock()
            resp_switch = loop.run_until_complete(dashboard.handle_switch(req_switch))
            data_switch = json.loads(resp_switch.text)
            self.assertEqual(data_switch["status"], "ok")
            self.assertFalse(dashboard.is_paused)
            self.assertEqual(twitch_mock._state, State.CHANNEL_SWITCH)
        finally:
            loop.close()

        # 3. Test Discord Bot StatusView pause_button
        from discord_bot import DiscordBotService, StatusView
        bot_service = MagicMock(spec=DiscordBotService)
        bot_service.twitch = twitch_mock
        bot_service.is_authorized.return_value = True

        status_view = StatusView(bot_service)
        self.assertIsNotNone(status_view)
        pause_btns = [c for c in status_view.children if getattr(c, "custom_id", None) == "tdm_btn_pause"]
        self.assertEqual(len(pause_btns), 1)

        mock_interaction = MagicMock()
        mock_interaction.response = MagicMock()
        mock_interaction.response.send_message = AsyncMock()

        loop2 = asyncio.new_event_loop()
        try:
            # Miner starts unpaused
            twitch_mock.is_paused = False
            loop2.run_until_complete(status_view.pause_button.callback(mock_interaction))
            self.assertTrue(twitch_mock.is_paused)
            mock_interaction.response.send_message.assert_awaited()
            msg_text = mock_interaction.response.send_message.call_args[0][0]
            self.assertIn("paused", msg_text.lower())

            # Click again to resume
            loop2.run_until_complete(status_view.pause_button.callback(mock_interaction))
            self.assertFalse(twitch_mock.is_paused)
            msg_text_resumed = mock_interaction.response.send_message.call_args[0][0]
            self.assertIn("resumed", msg_text_resumed.lower())
        finally:
            loop2.close()


class TestNetworkResilienceAndLogging(unittest.TestCase):
    def test_twitch_network_healthy_property(self):
        """Verify is_network_healthy reflects recent errors and consecutive GQL failures"""
        from twitch import Twitch
        from time import time

        twitch_obj = Twitch.__new__(Twitch)
        twitch_obj._last_network_error_time = 0
        twitch_obj._consecutive_gql_failures = 0

        self.assertTrue(twitch_obj.is_network_healthy)

        # Failure 1: recent error
        twitch_obj._last_network_error_time = time()
        self.assertFalse(twitch_obj.is_network_healthy)

        # Failure 2: consecutive GQL failures
        twitch_obj._last_network_error_time = 0
        twitch_obj._consecutive_gql_failures = 2
        self.assertFalse(twitch_obj.is_network_healthy)

    def test_invalid_game_names_logged_as_info(self):
        """Verify _log_invalid_game_names logs suggestions at INFO level for priority, but not for exclude"""
        from twitch import Twitch
        import logging

        with self.assertLogs("TwitchDrops", level=logging.INFO) as log:
            Twitch._log_invalid_game_names(
                "priority",
                ["EA Sports College Football 26"],
                ["EA Sports College Football 27"]
            )
            self.assertTrue(any("INFO" in record for record in log.output))
            self.assertFalse(any("WARNING" in record for record in log.output))

        # For exclude games: verify no suggestions are logged
        with self.assertLogs("TwitchDrops", level=logging.INFO) as log:
            Twitch._log_invalid_game_names(
                "exclude",
                ["World of Tanks Console"],
                ["World of Tanks"]
            )
            # Should not contain "Did you mean"
            self.assertFalse(any("Did you mean" in record for record in log.output))


class TestJsonSaveAndPriorityRanking(unittest.TestCase):
    def test_json_save_ebusy_fallback(self):
        """Verify json_save handles Docker bind-mount EBUSY error with shutil.copyfile fallback"""
        import tempfile
        import errno
        from pathlib import Path
        from utils import json_save

        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = Path(tmpdir) / "settings.json"
            target_path.write_text("{}", encoding="utf8")

            # Mock replace to raise EBUSY
            original_replace = Path.replace
            replace_called = []

            def mock_replace(self, target):
                replace_called.append(True)
                err = OSError("Device or resource busy")
                err.errno = errno.EBUSY
                raise err

            data_to_save = {"priority": ["Rust", "Sea of Thieves"], "test": True}

            with patch.object(Path, "replace", side_effect=mock_replace, autospec=True):
                # Should not raise; should succeed via shutil.copyfile
                json_save(target_path, data_to_save)

            self.assertTrue(replace_called)
            import json
            loaded = json.loads(target_path.read_text(encoding="utf8"))
            self.assertEqual(loaded["priority"], ["Rust", "Sea of Thieves"])
            self.assertTrue(loaded["test"])
            # Ensure .new was cleaned up
            new_path = target_path.with_name(f"{target_path.name}.new")
            self.assertFalse(new_path.exists())

    def test_priority_ranking_add_and_move_logic(self):
        """Verify ranking operations: add with rank, move rank, top promotion"""
        priority = ["Rust", "Apex Legends", "Valorant"]

        # Insert new game at rank 2 (index 1)
        new_game = "Overwatch 2"
        rank = 2
        target_idx = max(0, min(rank - 1, len(priority)))
        priority.insert(target_idx, new_game)
        self.assertEqual(priority, ["Rust", "Overwatch 2", "Apex Legends", "Valorant"])

        # Move Valorant from rank 4 to rank 1
        game_to_move = "Valorant"
        priority.remove(game_to_move)
        target_idx = max(0, min(1 - 1, len(priority)))
        priority.insert(target_idx, game_to_move)
        self.assertEqual(priority, ["Valorant", "Rust", "Overwatch 2", "Apex Legends"])

        # Promote Apex Legends to top (rank 1)
        game_to_top = "Apex Legends"
        priority.remove(game_to_top)
        priority.insert(0, game_to_top)
        self.assertEqual(priority[0], "Apex Legends")


class TestBugFixesAndRegression(unittest.TestCase):
    """Regression tests for the 4 issues reported in user production."""

    def test_is_game_excluded_exact_and_wildcard(self):
        """Verify is_game_excluded supports exact matching, distinct editions, and wildcard patterns"""
        from utils import is_game_excluded

        # 1. Case-insensitive exact match
        self.assertTrue(is_game_excluded("World of Tanks", {"world of tanks"}))
        self.assertTrue(is_game_excluded("world of tanks", {"World of Tanks"}))
        self.assertTrue(is_game_excluded("World of Tanks: HEAT", {"World of Tanks: HEAT"}))

        # 2. Distinct editions are independent:
        # User wants to exclude "World of Tanks" while still allowing "World of Tanks: HEAT"
        self.assertFalse(is_game_excluded("World of Tanks: HEAT", {"World of Tanks"}))
        self.assertFalse(is_game_excluded("World of Tanks", {"World of Tanks: HEAT"}))
        self.assertFalse(is_game_excluded("Path of Exile 2", {"Path of Exile"}))

        # 3. Wildcard matching: "World of Tanks*" excludes all variants
        self.assertTrue(is_game_excluded("World of Tanks", {"World of Tanks*"}))
        self.assertTrue(is_game_excluded("World of Tanks: HEAT", {"World of Tanks*"}))
        self.assertTrue(is_game_excluded("World of Tanks Console", {"World of Tanks*"}))

        # 4. Negative matches: verify no false positives
        self.assertFalse(is_game_excluded("World of Warcraft", {"World of Tanks"}))
        self.assertFalse(is_game_excluded("Warframe", {"War"}))
        self.assertFalse(is_game_excluded("The Finals", {"The"}))

    def test_twitch_exclude_and_can_watch_logic(self):
        """Verify can_watch and priority calculation reject excluded games and priority overrides exclude"""
        from twitch import Twitch
        from channel import Channel
        from utils import Game
        from constants import PriorityMode, MAX_INT

        twitch_obj = Twitch.__new__(Twitch)
        twitch_obj.settings = MagicMock()
        twitch_obj.settings.exclude = {"World of Tanks: HEAT"}
        twitch_obj.settings.priority = ["Rainbow Six Siege"]
        twitch_obj.settings.priority_mode = PriorityMode.BALANCED
        twitch_obj.settings.enable_badges_emotes = True
        twitch_obj._channel_blacklist = {}
        twitch_obj._game_live_counts = {}

        wot_game = Game({"id": 1001, "displayName": "World of Tanks: HEAT", "name": "World of Tanks: HEAT"})
        r6_game = Game({"id": 1002, "displayName": "Rainbow Six Siege", "name": "Rainbow Six Siege"})

        twitch_obj.wanted_games = [r6_game]

        # Channel for World of Tanks: HEAT
        wot_channel = MagicMock(spec=Channel)
        wot_channel.id = 5001
        wot_channel.name = "wot_streamer"
        wot_channel.game = wot_game
        wot_channel.online = True
        wot_channel.drops_enabled = True

        # Mock campaign for WoT
        wot_campaign = MagicMock()
        wot_campaign.game = wot_game
        wot_campaign.has_badge_or_emote = False
        wot_campaign.can_earn.return_value = True
        twitch_obj.inventory = [wot_campaign]

        # 1. can_watch must be False for WoT channel even if not c.has_badge_or_emote
        self.assertFalse(twitch_obj.can_watch(wot_channel))

        # 2. get_balanced_priority must return inf for excluded game
        self.assertEqual(twitch_obj.get_balanced_priority(wot_channel), float('inf'))

        # 3. get_priority must return MAX_INT in non-balanced mode
        twitch_obj.settings.priority_mode = PriorityMode.PRIORITY_ONLY
        self.assertEqual(twitch_obj.get_priority(wot_channel), MAX_INT)

        # 4. PRIORITY ALWAYS OVERRIDES EXCLUDE:
        # If user puts "World of Tanks: HEAT" in priority list, is_game_excluded returns False!
        twitch_obj.settings.priority = ["World of Tanks: HEAT"]
        self.assertFalse(twitch_obj.is_game_excluded(wot_game))

    def test_web_dashboard_status_now_and_metrics(self):
        """Verify handle_status defines now correctly, serializes campaigns without NameError, and maps metrics"""
        import json
        from web_dashboard import WebDashboard
        from utils import Game
        from aiohttp.test_utils import make_mocked_request
        from datetime import datetime, timezone, timedelta

        mock_twitch = MagicMock()
        mock_twitch.settings.priority = ["Rust"]
        mock_twitch.settings.exclude = {"World of Tanks"}
        mock_twitch.settings.priority_mode.name = "BALANCED"
        mock_twitch.settings.discord_notifications_enabled = True
        mock_twitch.settings.discord_webhook_url = ""
        mock_twitch.settings.discord_summary_interval_minutes = 60
        mock_twitch.watching_channel.get_with_default.return_value = None
        mock_twitch.get_active_campaign.return_value = None
        mock_twitch._state.name = "IDLE"
        mock_twitch.is_network_healthy = True
        mock_twitch._consecutive_gql_failures = 0

        # Create active mock campaign with ends_at in future
        mock_camp = MagicMock()
        mock_camp.game = Game({"id": 1234, "displayName": "Rust", "name": "Rust"})
        mock_camp.name = "Rust Community Drops"
        mock_camp.claimed_drops = 7
        mock_camp.total_drops = 10
        mock_camp.progress = 0.7
        mock_camp.ends_at = datetime.now(timezone.utc) + timedelta(hours=24)
        mock_camp.active_drop = None
        mock_twitch.inventory = [mock_camp]

        # Mock metrics with stats
        mock_metrics = MagicMock()
        mock_metrics.get_stats.return_value = {
            "drops_claimed": 5,
            "drops_failed": 0,
            "uptime_hours": 12.5,
            "watch_success_rate": 98.5,
            "total_minutes_watched": 750,
        }
        mock_twitch.metrics = mock_metrics

        dashboard = WebDashboard(mock_twitch, host="127.0.0.1", port=8080)
        req = make_mocked_request("GET", "/api/status")

        loop = asyncio.new_event_loop()
        try:
            resp = loop.run_until_complete(dashboard.handle_status(req))
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.text)

            # Check that campaign calculation with now succeeded
            self.assertEqual(len(data["campaigns"]), 1)
            camp_data = data["campaigns"][0]
            self.assertEqual(camp_data["game"], "Rust")
            self.assertTrue(camp_data["time_left"].endswith("h left"))

            # Check metrics mapping and total claimed drops
            metrics_data = data["metrics"]
            self.assertEqual(metrics_data["drops_claimed"], 7)  # max(5, 7 from inventory)
            self.assertEqual(metrics_data["claimed_drops"], 7)
            self.assertEqual(metrics_data["watch_time_hours"], 12.5)
            self.assertEqual(metrics_data["success_rate"], 98.5)
        finally:
            loop.close()

    def test_discord_bot_interaction_timeout_handling(self):
        """Verify Discord bot suppresses 10062 Unknown Interaction errors cleanly"""
        from discord_bot import DiscordBotService
        from discord.app_commands import CommandInvokeError
        import discord

        mock_twitch = MagicMock()
        mock_twitch.settings.discord_bot_token = "MOCK_TOKEN"
        mock_twitch.settings.discord_owner_id = ""

        service = DiscordBotService(mock_twitch)

        # Mock an expired interaction
        mock_interaction = MagicMock()
        mock_interaction.command.name = "status"

        # Create mock 10062 NotFound error
        resp = MagicMock()
        resp.status = 404
        data = {"code": 10062, "message": "Unknown interaction"}
        not_found_err = discord.NotFound(resp, data)
        cmd_err = CommandInvokeError(mock_interaction.command, not_found_err)

        # Verify on_app_command_error handles 10062 without raising or calling followup
        loop = asyncio.new_event_loop()
        try:
            tree_err_handler = service.bot.tree.error
            # Call error handler
            loop.run_until_complete(service.bot.tree.on_error(mock_interaction, cmd_err))
            # Verify followup.send was NOT called (interaction already expired)
            mock_interaction.followup.send.assert_not_called()
        finally:
            loop.close()


class TestDashboardQOLFeatures(unittest.TestCase):
    def test_format_box_art(self):
        """Verify format_box_art correctly handles templates, dimensions, and fallbacks"""
        from web_dashboard import format_box_art

        # Templated dimensions
        tpl = "https://static-cdn.jtvnw.net/ttv-boxart/516575_{width}x{height}.jpg"
        self.assertEqual(format_box_art(tpl, 144, 192), "https://static-cdn.jtvnw.net/ttv-boxart/516575_144x192.jpg")
        self.assertEqual(format_box_art(tpl, 188, 250), "https://static-cdn.jtvnw.net/ttv-boxart/516575_188x250.jpg")

        # Un-dimensioned ttv-boxart
        undim = "https://static-cdn.jtvnw.net/ttv-boxart/516575.png"
        self.assertEqual(format_box_art(undim, 144, 192), "https://static-cdn.jtvnw.net/ttv-boxart/516575-144x192.png")

        # Mock and None fallbacks
        self.assertIsNone(format_box_art(None))
        self.assertIsNone(format_box_art(""))
        self.assertIsNone(format_box_art(MagicMock()))

    def test_active_farm_recognition_and_rich_metadata(self):
        """Verify dashboard status exposes active farm recognition, drop thumbnails, and box art"""
        from web_dashboard import WebDashboard
        import json
        from datetime import datetime, timezone, timedelta
        from aiohttp.test_utils import make_mocked_request

        mock_camp1 = MagicMock()
        mock_camp1.id = "camp-active-123"
        mock_camp1.name = "Rust Twitch Rivals"
        mock_camp1.game.name = "Rust"
        mock_camp1.image_url = "https://static-cdn.jtvnw.net/ttv-boxart/263490_{width}x{height}.jpg"
        mock_camp1.claimed_drops = 2
        mock_camp1.total_drops = 5
        mock_camp1.progress = 0.4
        mock_camp1.ends_at = datetime.now(timezone.utc) + timedelta(hours=10)

        mock_benefit = MagicMock()
        mock_benefit.name = "Exclusive Hazmat Skin"
        mock_benefit.image_url = "https://static-cdn.jtvnw.net/custom-drop-assets/hazmat.png"

        mock_drop = MagicMock()
        mock_drop.id = "drop-hazmat-999"
        mock_drop.name = "Hazmat Suit"
        mock_drop.rewards_text.return_value = "Exclusive Hazmat Skin"
        mock_drop.required_minutes = 120
        mock_drop.current_minutes = 60
        mock_drop.remaining_minutes = 60
        mock_drop.progress = 0.5
        mock_drop.benefits = [mock_benefit]
        mock_camp1.active_drop = mock_drop

        mock_camp2 = MagicMock()
        mock_camp2.id = "camp-idle-456"
        mock_camp2.name = "Warframe TennoCon"
        mock_camp2.game.name = "Warframe"
        mock_camp2.image_url = "https://static-cdn.jtvnw.net/ttv-boxart/warframe_{width}x{height}.jpg"
        mock_camp2.claimed_drops = 0
        mock_camp2.total_drops = 3
        mock_camp2.progress = 0.0
        mock_camp2.ends_at = datetime.now(timezone.utc) + timedelta(hours=48)
        mock_camp2.active_drop = None

        mock_channel = MagicMock()
        mock_channel.name = "shroud"
        mock_channel.viewers = 45120
        mock_channel.stream_title = "Rivals Tournament Day 2 !drops"
        mock_channel.game.name = "Rust"
        mock_channel.game.box_art_url = "https://static-cdn.jtvnw.net/ttv-boxart/263490_{width}x{height}.jpg"

        mock_twitch = MagicMock()
        mock_twitch.settings.priority = ["Rust"]
        mock_twitch.settings.exclude = set()
        mock_twitch.settings.priority_mode.name = "BALANCED"
        mock_twitch.settings.discord_notifications_enabled = True
        mock_twitch.settings.discord_webhook_url = ""
        mock_twitch.settings.discord_summary_interval_minutes = 60
        mock_twitch.watching_channel.get_with_default.return_value = mock_channel
        mock_twitch.get_active_campaign.return_value = mock_camp1
        mock_twitch.inventory = [mock_camp1, mock_camp2]
        mock_twitch.metrics.get_stats.return_value = {"drops_claimed": 15}
        mock_twitch._state.name = "ACTIVE"
        mock_twitch.is_network_healthy = True
        mock_twitch._consecutive_gql_failures = 0

        dashboard = WebDashboard(mock_twitch, host="127.0.0.1", port=8080)
        req = make_mocked_request("GET", "/api/status")

        loop = asyncio.new_event_loop()
        try:
            resp = loop.run_until_complete(dashboard.handle_status(req))
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.text)

            # Active channel data
            self.assertIsNotNone(data["channel"])
            self.assertEqual(data["channel"]["name"], "shroud")
            self.assertEqual(data["channel"]["viewers"], 45120)
            self.assertEqual(data["channel"]["title"], "Rivals Tournament Day 2 !drops")
            self.assertTrue("144x192" in data["channel"]["box_art_url"])

            # Active drop thumbnail and benefit info
            self.assertIsNotNone(data["drop"])
            self.assertEqual(data["drop"]["benefit_name"], "Exclusive Hazmat Skin")
            self.assertEqual(data["drop"]["benefit_image"], "https://static-cdn.jtvnw.net/custom-drop-assets/hazmat.png")
            self.assertEqual(data["drop"]["remaining_minutes"], 60)

            # Active farm recognition across campaigns
            camps = data["campaigns"]
            self.assertEqual(len(camps), 2)
            active_c = next(c for c in camps if c["id"] == "camp-active-123")
            idle_c = next(c for c in camps if c["id"] == "camp-idle-456")
            self.assertTrue(active_c["is_currently_farming"])
            self.assertFalse(idle_c["is_currently_farming"])
            self.assertTrue("144x192" in active_c["box_art_url"])
        finally:
            loop.close()

    def test_games_catalog_for_autocomplete_and_quick_chips(self):
        """Verify GET /api/settings options includes rich games_catalog with box art and claim history"""
        from web_dashboard import WebDashboard
        import json
        from datetime import datetime, timezone, timedelta
        from aiohttp.test_utils import make_mocked_request

        mock_camp1 = MagicMock()
        mock_camp1.game.name = "Rust"
        mock_camp1.image_url = "https://static-cdn.jtvnw.net/ttv-boxart/rust_{width}x{height}.jpg"
        mock_camp1.active = True
        mock_camp1.linked = True
        mock_camp1.claimed_drops = 3
        mock_camp1.total_drops = 3
        mock_camp1.ends_at = datetime.now(timezone.utc) + timedelta(hours=5)

        mock_camp2 = MagicMock()
        mock_camp2.game.name = "Warframe"
        mock_camp2.image_url = None
        mock_camp2.active = False
        mock_camp2.linked = True
        mock_camp2.claimed_drops = 1
        mock_camp2.total_drops = 2
        mock_camp2.ends_at = None

        mock_settings = MagicMock()
        mock_settings.priority_mode.name = "BALANCED"
        mock_settings.priority = ["Rust"]
        mock_settings.exclude = {"Fortnite*"}
        mock_settings.discord_webhook_url = ""
        mock_settings.discord_notifications_enabled = True
        mock_settings.discord_summary_interval_minutes = 60
        mock_settings.web_port = 8080
        mock_settings.web_password = ""

        mock_twitch = MagicMock()
        mock_twitch.settings = mock_settings
        mock_twitch.inventory = [mock_camp1, mock_camp2]

        dashboard = WebDashboard(mock_twitch, host="127.0.0.1", port=8080)
        req = make_mocked_request("GET", "/api/settings")

        loop = asyncio.new_event_loop()
        try:
            resp = loop.run_until_complete(dashboard.handle_get_settings(req))
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.text)
            self.assertIn("games_catalog", data["options"])
            catalog = data["options"]["games_catalog"]
            self.assertTrue(len(catalog) >= 3)  # Rust, Warframe, Fortnite*

            rust_entry = next(g for g in catalog if g["name"] == "Rust")
            self.assertTrue(rust_entry["is_active"])
            self.assertEqual(rust_entry["claimed_drops"], 3)
            self.assertIsNotNone(rust_entry["box_art_url"])

            # Verify active games are sorted first in catalog
            self.assertTrue(catalog[0]["is_active"])
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
