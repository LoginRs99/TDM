from __future__ import annotations

from typing import Any, TypedDict, TYPE_CHECKING
import logging
import os
from yarl import URL

from utils import json_load, json_save
from constants import SETTINGS_PATH, PriorityMode

if TYPE_CHECKING:
    from main import ParsedArgs

# Initialize logger
logger = logging.getLogger("TwitchDrops")

class SettingsFile(TypedDict):
    proxy: URL
    exclude: set[str]
    priority: list[str]
    connection_quality: int
    priority_mode: PriorityMode
    stale_stream_timeout_minutes: int
    maintenance_interval_minutes: int
    # Discord / Logging
    discord_webhook_url: str
    discord_summary_interval_minutes: int
    logging_level: str
    enable_badges_emotes: bool
    available_drops_check: bool
    # Performance
    enable_watch_stats: bool

# These defaults ensure the Optimized Logic works immediately
default_settings: SettingsFile = {
    "proxy": URL(),
    "priority": [],
    "exclude": set(),
    "connection_quality": 1,
    # Default to BALANCED (3) to use the new smart logic
    "priority_mode": PriorityMode.BALANCED, 
    "maintenance_interval_minutes": 20,
    "stale_stream_timeout_minutes": 5,
    "discord_webhook_url": "",
    "discord_summary_interval_minutes": 60,
    "logging_level": "INFO",
    "enable_badges_emotes": False,
    "available_drops_check": False,
    "enable_watch_stats": False,  # Disabled by default for production
}


class Settings:
    # from args
    log: bool
    dump: bool
    # args properties
    debug_ws: int
    debug_gql: int
    logging_level: str
    # from settings file
    proxy: URL
    exclude: set[str]
    priority: list[str]
    connection_quality: int
    priority_mode: PriorityMode
    discord_webhook_url: str
    discord_summary_interval_minutes: int
    stale_stream_timeout_minutes: int
    maintenance_interval_minutes: int
    enable_badges_emotes: bool
    available_drops_check: bool
    enable_watch_stats: bool
   

    PASSTHROUGH = ("_settings", "_args", "_altered")

    def __init__(self, args: ParsedArgs):
        self._settings: SettingsFile = json_load(SETTINGS_PATH, default_settings)
        self._args: ParsedArgs = args
        self._altered: bool = False
        
        # Validate and override with environment variables
        self._load_env_overrides()
        
        # Validate settings after loading
        self._validate_settings()
    
    def _load_env_overrides(self):
        """Load and validate environment variable overrides"""
        
        # Discord Webhook URL
        if webhook := os.getenv('DISCORD_WEBHOOK_URL'):
            webhook = webhook.strip()
            if webhook.startswith('https://discord.com/api/webhooks/'):
                self._settings['discord_webhook_url'] = webhook
                logger.info("Discord webhook URL loaded from environment")
            else:
                logger.warning(
                    "DISCORD_WEBHOOK_URL doesn't look valid - ignoring. "
                    "Should start with: https://discord.com/api/webhooks/"
                )
        
        # Discord Summary Interval
        if interval := os.getenv('DISCORD_SUMMARY_INTERVAL'):
            try:
                interval_int = int(interval)
                if 1 <= interval_int <= 1440:  # 1 minute to 24 hours
                    self._settings['discord_summary_interval_minutes'] = interval_int
                    logger.info(f"Discord summary interval set to {interval_int} minutes")
                else:
                    logger.warning(
                        f"DISCORD_SUMMARY_INTERVAL out of range ({interval_int}). "
                        f"Must be 1-1440 minutes. Using default: "
                        f"{default_settings['discord_summary_interval_minutes']}"
                    )
            except ValueError:
                logger.warning(
                    f"DISCORD_SUMMARY_INTERVAL is not a valid integer: '{interval}'. "
                    f"Using default: {default_settings['discord_summary_interval_minutes']}"
                )
        
        # Logging Level
        if log_level := os.getenv('LOGGING_LEVEL'):
            log_level = log_level.upper()
            valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR']
            if log_level in valid_levels:
                self._settings['logging_level'] = log_level
                logger.info(f"Logging level set to {log_level}")
            else:
                logger.warning(
                    f"Invalid LOGGING_LEVEL: '{log_level}'. "
                    f"Must be one of: {', '.join(valid_levels)}. "
                    f"Using default: {default_settings['logging_level']}"
                )
        
        # Maintenance Interval
        if maint := os.getenv('MAINTENANCE_INTERVAL_MINUTES'):
            try:
                maint_int = int(maint)
                if 5 <= maint_int <= 120:  # 5 minutes to 2 hours
                    self._settings['maintenance_interval_minutes'] = maint_int
                    logger.info(f"Maintenance interval set to {maint_int} minutes")
                else:
                    logger.warning(
                        f"MAINTENANCE_INTERVAL_MINUTES out of range: {maint_int}"
                    )
            except ValueError:
                logger.warning(f"Invalid MAINTENANCE_INTERVAL_MINUTES: '{maint}'")
        
        # Priority Mode
        if priority_mode := os.getenv('PRIORITY_MODE'):
            mode_map = {
                'PRIORITY_ONLY': PriorityMode.PRIORITY_ONLY,
                'ENDING_SOONEST': PriorityMode.ENDING_SOONEST,
                'LOW_AVBL_FIRST': PriorityMode.LOW_AVBL_FIRST,
                'BALANCED': PriorityMode.BALANCED,
                '0': PriorityMode.PRIORITY_ONLY,
                '1': PriorityMode.ENDING_SOONEST,
                '2': PriorityMode.LOW_AVBL_FIRST,
                '3': PriorityMode.BALANCED,
            }
            if priority_mode in mode_map:
                self._settings['priority_mode'] = mode_map[priority_mode]
                logger.info(f"Priority mode set to {priority_mode}")
            else:
                logger.warning(f"Invalid PRIORITY_MODE: '{priority_mode}'")
    
    def _validate_settings(self):
        """Validate settings values are in acceptable ranges"""
        
        # Validate intervals are positive
        if self._settings['maintenance_interval_minutes'] < 5:
            logger.warning(
                "Maintenance interval is very low (< 5 minutes). "
                "This may cause excessive API calls."
            )
        
        if self._settings['stale_stream_timeout_minutes'] < 3:
            logger.warning(
                "Stale stream timeout is very low (< 3 minutes). "
                "This may cause premature channel switches."
            )
        
        # Validate connection quality
        if self._settings['connection_quality'] not in [1, 2, 3]:
            logger.warning(
                f"Invalid connection_quality: {self._settings['connection_quality']}. "
                f"Should be 1 (low), 2 (medium), or 3 (high). Using 1."
            )
            self._settings['connection_quality'] = 1

    def __getattr__(self, name: str, /) -> Any:
        if name in self.PASSTHROUGH:
            return getattr(super(), name)
        elif hasattr(self._args, name):
            return getattr(self._args, name)
        elif name in self._settings:
            return self._settings[name]  # type: ignore[literal-required]
        return getattr(super(), name)

    def __setattr__(self, name: str, value: Any, /) -> None:
        if name in self.PASSTHROUGH:
            return super().__setattr__(name, value)
        elif name in self._settings:
            self._settings[name] = value  # type: ignore[literal-required]
            self._altered = True
            return
        raise TypeError(f"{name} is missing a custom setter")

    def __delattr__(self, name: str, /) -> None:
        raise RuntimeError("settings can't be deleted")

    def alter(self) -> None:
        self._altered = True

    def save(self, *, force: bool = False) -> None:
        """
        Saves settings to disk.
        Handles Read-Only filesystems gracefully by catching OSErrors.
        """
        if self._altered or force:
            try:
                json_save(SETTINGS_PATH, self._settings, sort=True)
                self._altered = False
                logger.debug("Settings saved to disk")
            except OSError as e:
                if self._altered:
                    logger.warning(
                        f"Settings changed but could not be saved "
                        f"(Read-Only filesystem?): {e}"
                    )
                else:
                    logger.debug(f"Skipping settings save: {e}")
            except Exception as e:
                logger.error(f"Failed to save settings: {e}")
    
    def get_summary(self) -> str:
        """Get a human-readable summary of current settings"""
        lines = [
            "=== Current Settings ===",
            f"Priority Mode: {self.priority_mode.name}",
            f"Priority Games: {len(self.priority)} configured",
            f"Excluded Games: {len(self.exclude)} configured",
            f"Maintenance Interval: {self.maintenance_interval_minutes} minutes",
            f"Discord Notifications: {'Enabled' if self.discord_webhook_url else 'Disabled'}",
        ]
        
        if self.discord_webhook_url:
            lines.append(
                f"  Summary Interval: {self.discord_summary_interval_minutes} minutes"
            )
        
        lines.extend([
            f"Logging Level: {self.logging_level}",
            f"Watch Stats: {'Enabled' if self.enable_watch_stats else 'Disabled'}",
            f"Available Drops Check: {'Enabled' if self.available_drops_check else 'Disabled'}",
        ])
        
        return "\n".join(lines)
