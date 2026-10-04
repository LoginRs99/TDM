"""
Two-way Interactive Discord Bot for Twitch Drops Miner.
Provides Slash Commands, Buttons, Select Menus, Autocomplete and Live Status.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from constants import PriorityMode, State, WORKING_DIR

if TYPE_CHECKING:
    from twitch import Twitch

logger = logging.getLogger("TwitchDrops")

TOKEN_2FA_PATH = WORKING_DIR / "files" / "2fa.token"


def make_progress_bar(percent: int, length: int = 10) -> str:
    filled = int(length * percent / 100)
    return "█" * filled + "░" * (length - filled)


class StatusView(discord.ui.View):
    """Interactive action buttons attached to /status embed."""

    def __init__(self, bot_service: DiscordBotService):
        super().__init__(timeout=None)
        self.bot_service = bot_service

    def _check_auth(self, interaction: discord.Interaction) -> bool:
        return self.bot_service.is_authorized(interaction)

    @discord.ui.button(label="Switch Channel", style=discord.ButtonStyle.primary, emoji="🔄", custom_id="tdm_btn_switch")
    async def switch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_auth(interaction):
            await interaction.response.send_message("⛔ You are not authorized to control this miner.", ephemeral=True)
            return

        self.bot_service.twitch.stop_watching()
        self.bot_service.twitch.change_state(State.CHANNEL_SWITCH)
        await interaction.response.send_message("🔄 Channel switch requested! Miner is switching stream...", ephemeral=True)

    @discord.ui.button(label="Refresh Campaigns", style=discord.ButtonStyle.secondary, emoji="📦", custom_id="tdm_btn_refresh")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_auth(interaction):
            await interaction.response.send_message("⛔ You are not authorized to control this miner.", ephemeral=True)
            return

        self.bot_service.twitch.change_state(State.INVENTORY_FETCH)
        await interaction.response.send_message("📦 Refreshing Twitch drop campaigns...", ephemeral=True)

    @discord.ui.button(label="Refresh Cookies", style=discord.ButtonStyle.success, emoji="🔐", custom_id="tdm_btn_cookies")
    async def cookies_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_auth(interaction):
            await interaction.response.send_message("⛔ You are not authorized to control this miner.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            from cookie_refresher import CookieRefresher
            refresher = CookieRefresher()
            ok = await refresher.refresh_session(headless=True)
            if ok:
                self.bot_service.twitch.check_cookies_updated()
                await interaction.followup.send("✅ Playwright refreshed cookies.jar successfully!", ephemeral=True)
            else:
                await interaction.followup.send("⚠️ Cookie refresh failed. Browser session may require login.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error refreshing cookies: {e}", ephemeral=True)


class ModeSelect(discord.ui.Select):
    """Dropdown menu to select PriorityMode."""

    def __init__(self, bot_service: DiscordBotService):
        self.bot_service = bot_service
        current_mode = bot_service.twitch.settings.priority_mode
        options = [
            discord.SelectOption(
                label="BALANCED (3)",
                value="BALANCED",
                description="Smart ranking: Priority games + scarce/urgent drops",
                default=current_mode == PriorityMode.BALANCED,
            ),
            discord.SelectOption(
                label="PRIORITY_ONLY (0)",
                value="PRIORITY_ONLY",
                description="Strictly mine priority list games only",
                default=current_mode == PriorityMode.PRIORITY_ONLY,
            ),
            discord.SelectOption(
                label="ENDING_SOONEST (1)",
                value="ENDING_SOONEST",
                description="Mine campaigns ending the earliest",
                default=current_mode == PriorityMode.ENDING_SOONEST,
            ),
            discord.SelectOption(
                label="LOW_AVBL_FIRST (2)",
                value="LOW_AVBL_FIRST",
                description="Mine campaigns with lowest availability ratio",
                default=current_mode == PriorityMode.LOW_AVBL_FIRST,
            ),
        ]
        super().__init__(placeholder="Select Priority Mode...", options=options, custom_id="tdm_select_mode")

    async def callback(self, interaction: discord.Interaction):
        if not self.bot_service.is_authorized(interaction):
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
            return

        mode_name = self.values[0]
        mode_val = PriorityMode[mode_name]
        self.bot_service.twitch.settings.priority_mode = mode_val
        self.bot_service.twitch.settings.alter()
        self.bot_service.twitch.settings.save()
        self.bot_service.twitch.stop_watching()
        self.bot_service.twitch.change_state(State.CHANNEL_SWITCH)

        await interaction.response.send_message(
            f"✅ **Priority Mode updated to `{mode_name}`!** Miner state updated.",
            ephemeral=True,
        )


class SettingsView(discord.ui.View):
    def __init__(self, bot_service: DiscordBotService):
        super().__init__(timeout=180)
        self.add_item(ModeSelect(bot_service))


class DiscordBotService:
    """Manages the two-way Discord bot lifecycle and slash commands."""

    def __init__(self, twitch: Twitch):
        self.twitch = twitch
        self.token: str = getattr(twitch.settings, "discord_bot_token", "").strip()
        self.owner_id: str = getattr(twitch.settings, "discord_owner_id", "").strip()
        
        intents = discord.Intents.default()
        self.bot = commands.Bot(command_prefix="!", intents=intents)
        self._task: asyncio.Task | None = None
        self._setup_events_and_commands()

    def is_authorized(self, interaction: discord.Interaction) -> bool:
        if self.owner_id:
            return str(interaction.user.id) == self.owner_id
        # Fallback if owner_id not set: allow only server administrators or DMs with bot
        if interaction.guild is None:
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.administrator)

    def _setup_events_and_commands(self):
        bot = self.bot
        service = self

        @bot.event
        async def on_ready():
            logger.info(f"Discord Bot logged in as {bot.user} (ID: {bot.user.id})")
            try:
                synced = await bot.tree.sync()
                logger.info(f"Synced {len(synced)} Discord slash commands")
            except Exception as e:
                logger.error(f"Failed to sync slash commands: {e}")

        # --- /status Command ---
        @bot.tree.command(name="status", description="Show live mining status, current channel and progress")
        async def cmd_status(interaction: discord.Interaction):
            wc = service.twitch.watching_channel.get_with_default(None)
            state_name = service.twitch._state.name if hasattr(service.twitch, "_state") else "UNKNOWN"
            mode_name = service.twitch.settings.priority_mode.name

            embed = discord.Embed(
                title="⛏️ Twitch Drops Miner - Live Status",
                color=0x9146FF if wc else 0x36393F,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Status", value=f"`{state_name}`", inline=True)
            embed.add_field(name="Mode", value=f"`{mode_name}`", inline=True)

            if metrics := getattr(service.twitch, "metrics", None):
                stats = metrics.get_stats()
                embed.add_field(name="Uptime", value=f"{stats.get('uptime_hours', 0):.1f}h", inline=True)
                embed.add_field(name="Claimed", value=str(stats.get("drops_claimed", 0)), inline=True)
                embed.add_field(name="Success Rate", value=f"{stats.get('watch_success_rate', 100):.0f}%", inline=True)
                embed.add_field(name="Total Watched", value=f"{stats.get('total_minutes_watched', 0)} min", inline=True)

            if wc:
                embed.add_field(name="📺 Channel", value=f"[{wc.name}]({wc.url})", inline=True)
                embed.add_field(name="🎮 Game", value=wc.game.name if wc.game else "N/A", inline=True)

                active_campaign = service.twitch.get_active_campaign(wc)
                if active_campaign and active_campaign.active_drop:
                    drop = active_campaign.active_drop
                    pct = int(drop.progress * 100) if drop.progress else 0
                    bar = make_progress_bar(pct, 12)
                    embed.add_field(
                        name=f"🎁 Drop: {drop.name}",
                        value=f"`{bar}` **{pct}%** ({drop.current_minutes}/{drop.required_minutes}m)\n"
                              f"Rewards: *{drop.rewards_text()}*\n"
                              f"Campaign: **{active_campaign.claimed_drops}/{active_campaign.total_drops}** claimed",
                        inline=False,
                    )
            else:
                embed.add_field(name="📺 Channel", value="*Idle / Searching for streams...*", inline=False)

            view = StatusView(service)
            await interaction.response.send_message(embed=embed, view=view)

        # --- /games Command ---
        @bot.tree.command(name="games", description="List active Twitch drop campaigns, priority and excluded status")
        async def cmd_games(interaction: discord.Interaction):
            now = datetime.now(timezone.utc)
            priority_set = set(service.twitch.settings.priority)
            exclude_set = service.twitch.settings.exclude

            embed = discord.Embed(
                title="🎮 Twitch Drops Campaigns Overview",
                color=0x5865F2,
                timestamp=now,
            )

            lines = []
            for c in sorted(service.twitch.inventory, key=lambda x: (x.game.name not in priority_set, x.ends_at)):
                hours = (c.ends_at - now).total_seconds() / 3600
                time_str = f"{hours:.1f}h left" if hours > 0 else "Ending"
                
                if c.game.name in priority_set:
                    prefix = "⭐ **[PRIORITY]**"
                elif c.game.name in exclude_set:
                    prefix = "🚫 **[EXCLUDED]**"
                else:
                    prefix = "•"

                lines.append(f"{prefix} **{c.game.name}**: {c.name} ({c.claimed_drops}/{c.total_drops} drops, {time_str})")

            if not lines:
                embed.description = "No active campaigns found in current inventory."
            else:
                # Truncate if over discord 4096 desc limit
                desc = "\n".join(lines[:25])
                if len(lines) > 25:
                    desc += f"\n*... and {len(lines) - 25} more campaigns*"
                embed.description = desc

            await interaction.response.send_message(embed=embed)

        # --- /priority Group ---
        priority_group = app_commands.Group(name="priority", description="Manage game priority list")

        async def game_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
            names = sorted({c.game.name for c in service.twitch.inventory})
            return [
                app_commands.Choice(name=name, value=name)
                for name in names if current.lower() in name.lower()
            ][:25]

        async def priority_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
            names = service.twitch.settings.priority
            return [
                app_commands.Choice(name=name, value=name)
                for name in names if current.lower() in name.lower()
            ][:25]

        @priority_group.command(name="list", description="Show ordered priority games")
        async def priority_list(interaction: discord.Interaction):
            priority = service.twitch.settings.priority
            if not priority:
                await interaction.response.send_message("ℹ️ No priority games configured (using auto selection).", ephemeral=True)
                return

            msg = "**⭐ Priority Games List:**\n"
            for i, name in enumerate(priority, 1):
                msg += f"**{i}.** {name}\n"
            await interaction.response.send_message(msg, ephemeral=True)

        @priority_group.command(name="add", description="Add a game to the priority list")
        @app_commands.autocomplete(game=game_autocomplete)
        async def priority_add(interaction: discord.Interaction, game: str):
            if not service.is_authorized(interaction):
                await interaction.response.send_message("⛔ Unauthorized.", ephemeral=True)
                return

            game_clean = game.strip()
            if game_clean in service.twitch.settings.priority:
                await interaction.response.send_message(f"ℹ️ `{game_clean}` is already in the priority list.", ephemeral=True)
                return

            service.twitch.settings.priority.append(game_clean)
            service.twitch.settings.alter()
            service.twitch.settings.save()
            service.twitch.stop_watching()
            service.twitch.change_state(State.GAMES_UPDATE)

            await interaction.response.send_message(
                f"✅ Added **{game_clean}** to Priority list! (Position #{len(service.twitch.settings.priority)})",
                ephemeral=True,
            )

        @priority_group.command(name="remove", description="Remove a game from the priority list")
        @app_commands.autocomplete(game=priority_autocomplete)
        async def priority_remove(interaction: discord.Interaction, game: str):
            if not service.is_authorized(interaction):
                await interaction.response.send_message("⛔ Unauthorized.", ephemeral=True)
                return

            game_clean = game.strip()
            if game_clean not in service.twitch.settings.priority:
                await interaction.response.send_message(f"⚠️ `{game_clean}` is not in the priority list.", ephemeral=True)
                return

            service.twitch.settings.priority.remove(game_clean)
            service.twitch.settings.alter()
            service.twitch.settings.save()
            service.twitch.stop_watching()
            service.twitch.change_state(State.GAMES_UPDATE)

            await interaction.response.send_message(f"🗑️ Removed **{game_clean}** from Priority list.", ephemeral=True)

        @priority_group.command(name="clear", description="Clear all priority games")
        async def priority_clear(interaction: discord.Interaction):
            if not service.is_authorized(interaction):
                await interaction.response.send_message("⛔ Unauthorized.", ephemeral=True)
                return

            service.twitch.settings.priority.clear()
            service.twitch.settings.alter()
            service.twitch.settings.save()
            service.twitch.stop_watching()
            service.twitch.change_state(State.GAMES_UPDATE)

            await interaction.response.send_message("🧹 Priority list cleared! Returning to automatic selection.", ephemeral=True)

        bot.tree.add_command(priority_group)

        # --- /exclude Group ---
        exclude_group = app_commands.Group(name="exclude", description="Manage excluded games")

        async def exclude_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
            names = sorted(service.twitch.settings.exclude)
            return [
                app_commands.Choice(name=name, value=name)
                for name in names if current.lower() in name.lower()
            ][:25]

        @exclude_group.command(name="list", description="Show excluded games")
        async def exclude_list(interaction: discord.Interaction):
            exclude = service.twitch.settings.exclude
            if not exclude:
                await interaction.response.send_message("ℹ️ No excluded games configured.", ephemeral=True)
                return

            msg = "**🚫 Excluded Games:**\n" + "\n".join(f"• {g}" for g in sorted(exclude))
            await interaction.response.send_message(msg, ephemeral=True)

        @exclude_group.command(name="add", description="Exclude a game from being watched")
        @app_commands.autocomplete(game=game_autocomplete)
        async def exclude_add(interaction: discord.Interaction, game: str):
            if not service.is_authorized(interaction):
                await interaction.response.send_message("⛔ Unauthorized.", ephemeral=True)
                return

            game_clean = game.strip()
            service.twitch.settings.exclude.add(game_clean)
            service.twitch.settings.alter()
            service.twitch.settings.save()
            service.twitch.stop_watching()
            service.twitch.change_state(State.GAMES_UPDATE)

            await interaction.response.send_message(f"🚫 Added **{game_clean}** to Excluded list.", ephemeral=True)

        @exclude_group.command(name="remove", description="Remove a game from excluded list")
        @app_commands.autocomplete(game=exclude_autocomplete)
        async def exclude_remove(interaction: discord.Interaction, game: str):
            if not service.is_authorized(interaction):
                await interaction.response.send_message("⛔ Unauthorized.", ephemeral=True)
                return

            game_clean = game.strip()
            service.twitch.settings.exclude.discard(game_clean)
            service.twitch.settings.alter()
            service.twitch.settings.save()
            service.twitch.stop_watching()
            service.twitch.change_state(State.GAMES_UPDATE)

            await interaction.response.send_message(f"✅ Removed **{game_clean}** from Excluded list.", ephemeral=True)

        bot.tree.add_command(exclude_group)

        # --- /settings Command ---
        @bot.tree.command(name="settings", description="View and configure miner settings interactively")
        async def cmd_settings(interaction: discord.Interaction):
            if not service.is_authorized(interaction):
                await interaction.response.send_message("⛔ Unauthorized.", ephemeral=True)
                return

            settings = service.twitch.settings
            embed = discord.Embed(
                title="⚙️ Twitch Drops Miner - Settings",
                color=0xFEE75C,
            )
            embed.add_field(name="Priority Mode", value=f"`{settings.priority_mode.name}`", inline=True)
            embed.add_field(name="Maintenance Interval", value=f"`{settings.maintenance_interval_minutes}m`", inline=True)
            embed.add_field(name="Stale Timeout", value=f"`{settings.stale_stream_timeout_minutes}m`", inline=True)
            embed.add_field(name="Priority Games", value=str(len(settings.priority)), inline=True)
            embed.add_field(name="Excluded Games", value=str(len(settings.exclude)), inline=True)
            embed.add_field(name="Auto Cookie Refresh", value=f"`{getattr(settings, 'auto_cookie_refresh', False)}`", inline=True)

            view = SettingsView(service)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        # --- /2fa Command ---
        @bot.tree.command(name="2fa", description="Provide 2FA token for Playwright login")
        async def cmd_2fa(interaction: discord.Interaction, code: str):
            if not service.is_authorized(interaction):
                await interaction.response.send_message("⛔ Unauthorized.", ephemeral=True)
                return

            import re
            token_clean = re.sub(r'[^a-zA-Z0-9]', '', code.strip())
            if not token_clean or len(token_clean) > 16:
                await interaction.response.send_message("❌ Invalid 2FA code format. Must be alphanumeric (max 16 chars).", ephemeral=True)
                return

            TOKEN_2FA_PATH.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_2FA_PATH.write_text(token_clean, encoding="utf-8")
            await interaction.response.send_message(f"🔐 2FA token `{token_clean}` delivered to Playwright login runner!", ephemeral=True)

    async def start(self):
        if not self.token:
            logger.info("Discord Bot Token not configured. Interactive Bot disabled.")
            return

        logger.info("Starting interactive Discord Bot...")
        self._task = asyncio.create_task(self.bot.start(self.token))

    async def stop(self):
        if self.bot and not self.bot.is_closed():
            await self.bot.close()
            logger.info("Discord Bot stopped")
        if self._task and not self._task.done():
            self._task.cancel()


_bot_instance: DiscordBotService | None = None


async def start_discord_bot(twitch: Twitch) -> DiscordBotService | None:
    global _bot_instance
    token = getattr(twitch.settings, "discord_bot_token", "").strip()
    if not token:
        return None

    if _bot_instance is None:
        _bot_instance = DiscordBotService(twitch)
        await _bot_instance.start()
    return _bot_instance


async def stop_discord_bot():
    global _bot_instance
    if _bot_instance is not None:
        await _bot_instance.stop()
        _bot_instance = None
