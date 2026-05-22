"""
Configuration validation system for Twitch Drops Miner
Validates required files, settings, and environment before startup
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from constants import COOKIES_PATH, LOG_PATH, SETTINGS_PATH

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
        if not COOKIES_PATH.exists():
            self.issues.append(f"Missing {COOKIES_PATH.name} - Twitch authentication cookies (critical)")
            all_ok = False
        if not SETTINGS_PATH.exists():
            self.warnings.append(f"Missing {SETTINGS_PATH.name} - will be created with defaults")
        return all_ok

    def validate_cookies_file(self) -> bool:
        """
        Validate cookies.jar file.
        IMPORTANT: cookies.jar is a BINARY file (SQLite / pickle / Netscape format).
        Never open it as text ('r' mode) — always use binary ('rb') mode.
        """
        cookies_path = COOKIES_PATH

        if not cookies_path.exists():
            return False

        file_size = cookies_path.stat().st_size
        if file_size < 50:
            self.issues.append(f"{COOKIES_PATH.name} is too small (likely empty or corrupted)")
            return False

        # Read as BINARY — do NOT attempt UTF-8 decode
        try:
            with open(cookies_path, 'rb') as f:
                header = f.read(16)
            if len(header) == 0:
                self.issues.append(f"{COOKIES_PATH.name} is empty")
                return False
        except Exception as e:
            self.issues.append(f"Cannot access {COOKIES_PATH.name}: {e}")
            return False

        logger.debug(f"{COOKIES_PATH.name} validated ({file_size} bytes)")
        return True

    def validate_settings_file(self, settings: Settings) -> bool:
        """Settings value validation lives in Settings; avoid duplicating policy here."""
        return True

    def validate_directories(self) -> bool:
        """Ensure required directories exist and are writable"""
        all_ok = True
        for path in [LOG_PATH.parent, SETTINGS_PATH.parent]:
            if not path.exists():
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    self.issues.append(f"Cannot create {path} directory: {e}")
                    all_ok = False
                    continue
            test_file = path / f".write_test_{id(self)}"
            try:
                test_file.touch()
                test_file.unlink()
            except Exception as e:
                self.warnings.append(f"Directory {path} may not be writable: {e}")
        return all_ok

    def validate_environment(self) -> bool:
        """Validate environment variables"""
        import os
        tz = os.getenv('TZ', 'UTC')
        if tz != 'UTC':
            self.warnings.append(f"Timezone is {tz} (UTC recommended)")
        return True

    def validate_all(self, settings: Settings | None = None) -> bool:
        """Run all validation checks"""
        logger.info("Running configuration validation...")

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
            logger.error("Critical configuration issues detected:")
            for issue in self.issues:
                logger.error(f"   {issue}")

        if self.warnings:
            logger.warning("Configuration warnings:")
            for warning in self.warnings:
                logger.warning(f"   {warning}")

        if all_ok and not self.warnings:
            logger.info("Configuration validation passed - all checks OK")
        elif all_ok:
            logger.info("Configuration validation passed (with warnings)")
        else:
            logger.error("Configuration validation FAILED")

        return all_ok

    def get_validation_report(self) -> str:
        """Get a formatted validation report"""
        lines = ["=== Configuration Validation Report ==="]
        if self.issues:
            lines.append("\nCRITICAL ISSUES:")
            for issue in self.issues:
                lines.append(f"   {issue}")
        if self.warnings:
            lines.append("\nWARNINGS:")
            for warning in self.warnings:
                lines.append(f"   {warning}")
        if not self.issues and not self.warnings:
            lines.append("\nAll checks passed successfully")
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
