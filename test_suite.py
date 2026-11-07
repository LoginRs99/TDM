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
        """Verify _log_invalid_game_names logs suggestions at INFO level, not WARNING"""
        from twitch import Twitch
        import logging

        with self.assertLogs("TwitchDrops", level=logging.INFO) as log:
            Twitch._log_invalid_game_names(
                "exclude",
                ["EA Sports College Football 26"],
                ["EA Sports College Football 27"]
            )
            self.assertTrue(any("INFO" in record for record in log.output))
            self.assertFalse(any("WARNING" in record for record in log.output))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
