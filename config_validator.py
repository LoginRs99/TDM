"""
Configuration validation system for Twitch Drops Miner
Validates required files, settings, and environment before startup
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from settings import Settings

logger = logging.getLogger("TwitchDrops")


class ConfigValidator:
    """Validates application configuration and requirements"""

    def __init__(self):
        self.issues: list[str] = []
        self.warnings: list[str] = []

    def validate_required_files(self) -> bool:
        """Check that all required files exist"""
        all_ok = True
        if not Path("cookies.jar").exists():
            self.issues.append("❌ Missing cookies.jar - Twitch authentication cookies (CRITICAL)")
            all_ok = False
        if not Path("settings.json").exists():
            self.warnings.append("⚠️  Missing settings.json - will be created with defaults")
        return all_ok

    def validate_cookies_file(self) -> bool:
        """
        Validate cookies.jar file.
        IMPORTANT: cookies.jar is a BINARY file (SQLite / pickle / Netscape format).
        Never open it as text ('r' mode) — always use binary ('rb') mode.
        """
        cookies_path = Path("cookies.jar")

        if not cookies_path.exists():
            return False

        file_size = cookies_path.stat().st_size
        if file_size < 50:
            self.issues.append("❌ cookies.jar is too small (likely empty or corrupted)")
            return False

        # Read as BINARY — do NOT attempt UTF-8 decode
        try:
            with open(cookies_path, 'rb') as f:
                header = f.read(16)
            if len(header) == 0:
                self.issues.append("❌ cookies.jar is empty")
                return False
        except Exception as e:
            self.issues.append(f"❌ Cannot access cookies.jar: {e}")
            return False

        logger.debug(f"✅ cookies.jar validated ({file_size} bytes)")
        return True

    def validate_settings_file(self, settings: Settings) -> bool:
        """Validate settings.json configuration"""
        all_ok = True

        webhook = settings.discord_webhook_url
        if webhook and not webhook.startswith('https://discord.com/api/webhooks/'):
            self.warnings.append(
                "⚠️  Discord webhook URL looks invalid "
                "(expected: https://discord.com/api/webhooks/...)"
            )

        if settings.discord_summary_interval_minutes < 1:
            self.warnings.append("⚠️  Discord summary interval is less than 1 minute")

        if settings.maintenance_interval_minutes < 5:
            self.warnings.append("⚠️  Maintenance interval is very low (< 5 minutes)")

        if settings.stale_stream_timeout_minutes < 3:
            self.warnings.append("⚠️  Stale stream timeout is very low (< 3 minutes)")

        if settings.priority_mode.value not in [0, 1, 2, 3]:
            self.issues.append(f"❌ Invalid priority mode: {settings.priority_mode.value}")
            all_ok = False

        total_weight = (
            settings.priority_weight_preference +
            settings.priority_weight_urgency
        )
        if abs(total_weight - 100) > 5:
            self.warnings.append(
                f"⚠️  Priority weights don't sum to 100% "
                f"(preference={settings.priority_weight_preference}%, "
                f"urgency={settings.priority_weight_urgency}%, total={total_weight}%)"
            )

        return all_ok

    def validate_directories(self) -> bool:
        """Ensure required directories exist and are writable"""
        all_ok = True
        for dir_path in ["logs", "."]:
            path = Path(dir_path)
            if not path.exists() and dir_path != ".":
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    self.issues.append(f"❌ Cannot create {dir_path} directory: {e}")
                    all_ok = False
                    continue
            test_file = path / f".write_test_{id(self)}"
            try:
                test_file.touch()
                test_file.unlink()
            except Exception as e:
                self.warnings.append(f"⚠️  Directory {dir_path} may not be writable: {e}")
        return all_ok

    def validate_environment(self) -> bool:
        """Validate environment variables"""
        import os
        tz = os.getenv('TZ', 'UTC')
        if tz != 'UTC':
            self.warnings.append(f"⚠️  Timezone is {tz} (UTC recommended)")
        return True

    def validate_all(self, settings: Settings | None = None) -> bool:
        """Run all validation checks"""
        logger.info("🔍 Running configuration validation...")

        all_ok = True
        if not self.validate_required_files():
            all_ok = False
        if not self.validate_cookies_file():
            all_ok = False
        if not self.validate_directories():
            all_ok = False
        if settings and not self.validate_settings_file(settings):
            all_ok = False
        self.validate_environment()

        if self.issues:
            logger.error("❌ Critical configuration issues detected:")
            for issue in self.issues:
                logger.error(f"   {issue}")

        if self.warnings:
            logger.warning("⚠️  Configuration warnings:")
            for warning in self.warnings:
                logger.warning(f"   {warning}")

        if all_ok and not self.warnings:
            logger.info("✅ Configuration validation passed - all checks OK")
        elif all_ok:
            logger.info("✅ Configuration validation passed (with warnings)")
        else:
            logger.error("❌ Configuration validation FAILED")

        return all_ok

    def get_validation_report(self) -> str:
        """Get a formatted validation report"""
        lines = ["=== Configuration Validation Report ==="]
        if self.issues:
            lines.append("\n❌ CRITICAL ISSUES:")
            for issue in self.issues:
                lines.append(f"   {issue}")
        if self.warnings:
            lines.append("\n⚠️  WARNINGS:")
            for warning in self.warnings:
                lines.append(f"   {warning}")
        if not self.issues and not self.warnings:
            lines.append("\n✅ All checks passed successfully")
        return "\n".join(lines)


def startup_validation(settings: Settings | None = None) -> bool:
    """
    Run all validation checks at startup.
    Returns True if OK to proceed, False if critical issues block startup.
    """
    validator = ConfigValidator()
    passed = validator.validate_all(settings)
    if not passed:
        logger.error(
            "\n" + validator.get_validation_report() +
            "\n\nPlease fix the critical issues before starting the miner."
        )
    return passed
