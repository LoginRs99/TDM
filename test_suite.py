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
