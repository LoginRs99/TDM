"""
Playwright-based Zero-Touch Cookie Refresher & Headless Login for Twitch Drops Miner.
Handles automatic session renewal from persistent browser profile and interactive/automated login.
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import aiohttp
from yarl import URL

from constants import COOKIES_PATH, WORKING_DIR

logger = logging.getLogger("TwitchDrops")

DEFAULT_PROFILE_DIR = WORKING_DIR / "files" / "browser_profile"
SCREENSHOT_2FA_PATH = WORKING_DIR / "files" / "login_2fa.png"
TOKEN_2FA_PATH = WORKING_DIR / "files" / "2fa.token"

# Essential Chromium flags for reliable operation in Linux Docker/Portainer environments
DEFAULT_CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
]


class CookieRefresher:
    """Manages Twitch login and cookies extraction via Playwright."""

    def __init__(
        self,
        cookies_path: Path = COOKIES_PATH,
        user_data_dir: Path = DEFAULT_PROFILE_DIR,
    ):
        self.cookies_path = Path(cookies_path)
        self.user_data_dir = Path(user_data_dir)
        self.user_data_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def is_2fa_pending(cls) -> bool:
        """Returns True if a 2FA challenge is currently waiting for user verification."""
        return SCREENSHOT_2FA_PATH.exists()

    @staticmethod
    def _parse_netscape_cookies(content: str) -> list[dict[str, Any]]:
        """Parses Netscape / Mozilla cookie file format into cookie dictionaries."""
        cookies = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                domain, flag, path, secure, expiration, name, value = parts[:7]
                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                    "expires": int(expiration) if expiration.isdigit() else -1,
                    "secure": secure.lower() == "true",
                })
        return cookies

    @classmethod
    def convert_external_cookies_file(cls, source_path: Path, target_path: Path = COOKIES_PATH) -> bool:
        """
        Attempts to convert external cookies (Netscape format or JSON)
        into a valid aiohttp pickle CookieJar file.
        """
        if not source_path.exists():
            return False

        try:
            # 1. Try reading as text to see if it's Netscape or JSON
            text = source_path.read_text(encoding="utf-8", errors="ignore").strip()
            raw_cookies: list[dict[str, Any]] = []

            if text.startswith("[") or text.startswith("{"):
                # JSON format (e.g. from EditThisCookie / Cookie-Editor)
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    raw_cookies = parsed
                elif isinstance(parsed, dict) and "cookies" in parsed:
                    raw_cookies = parsed["cookies"]
            elif "twitch.tv" in text and ("\t" in text or "auth-token" in text):
                # Netscape cookie format
                raw_cookies = cls._parse_netscape_cookies(text)

            if raw_cookies:
                return cls.save_cookies_list(raw_cookies, target_path)

        except Exception as e:
            logger.debug(f"Could not convert cookies as text/JSON: {e}")

        # Already binary / pickle format or unrecognized
        return False

    @staticmethod
    def _create_cookie_jar() -> aiohttp.CookieJar:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
        return aiohttp.CookieJar(loop=loop)

    @classmethod
    def save_cookies_list(cls, cookies: list[dict[str, Any]], target_path: Path = COOKIES_PATH) -> bool:
        """
        Saves a list of cookie dictionaries to an aiohttp.CookieJar binary file.
        """
        try:
            jar = cls._create_cookie_jar()
            twitch_url = URL("https://www.twitch.tv")

            # Feed cookies to jar
            simple_cookies = {
                c["name"]: c["value"]
                for c in cookies
                if "name" in c and "value" in c
            }
            jar.update_cookies(simple_cookies, twitch_url)

            # Ensure parent directory exists
            target_path.parent.mkdir(parents=True, exist_ok=True)

            # Atomically save to prevent partial write
            temp_path = target_path.with_suffix(".tmp")
            jar.save(temp_path)
            temp_path.replace(target_path)

            logger.info(f"Successfully saved {len(simple_cookies)} cookies to {target_path.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to save cookies to {target_path}: {e}")
            return False

    async def extract_cookies_from_browser(self, headless: bool = True) -> list[dict[str, Any]]:
        """
        Launches Playwright with the persistent profile and extracts Twitch cookies.
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=headless,
                args=DEFAULT_CHROME_ARGS,
                viewport={"width": 1280, "height": 720},
            )

            try:
                page = context.pages[0] if context.pages else await context.new_page()
                logger.info("Navigating to Twitch to verify session...")
                await page.goto("https://www.twitch.tv", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

                cookies = await context.cookies(["https://www.twitch.tv"])
                return cookies
            finally:
                await context.close()

    async def refresh_session(self, headless: bool = True) -> bool:
        """
        Refreshes cookies using the persistent browser profile without user interaction.
        Returns True if auth-token and unique_id were found and saved.
        """
        try:
            logger.info("Starting automatic cookie refresh via Playwright...")
            cookies = await self.extract_cookies_from_browser(headless=headless)

            cookie_names = {c["name"] for c in cookies}
            if "auth-token" in cookie_names:
                logger.info("Found active auth-token in browser profile!")
                return self.save_cookies_list(cookies, self.cookies_path)
            else:
                logger.warning(
                    "No auth-token found in browser profile. "
                    "A fresh login is required via --login or by providing credentials."
                )
                return False
        except Exception as e:
            logger.error(f"Error during automatic cookie refresh: {e}", exc_info=True)
            return False

    async def login_with_credentials(
        self,
        username: str,
        password: str,
        headless: bool = True,
        two_factor_code: str | None = None,
        timeout: int = 120,
    ) -> bool:
        """
        Performs automated Twitch login using provided credentials and persistent profile.
        Supports 2FA either via pre-supplied code or by waiting for files/2fa.token.
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=headless,
                args=DEFAULT_CHROME_ARGS,
                viewport={"width": 1280, "height": 720},
            )

            try:
                page = context.pages[0] if context.pages else await context.new_page()
                logger.info("Navigating to Twitch login page...")
                await page.goto("https://www.twitch.tv/login", wait_until="networkidle", timeout=45000)
                await asyncio.sleep(2)

                # Fill username and password
                username_input = page.locator("input#login-username")
                password_input = page.locator("input#password-input")

                if await username_input.count() > 0:
                    logger.info("Filling login credentials...")
                    await username_input.fill(username)
                    await password_input.fill(password)
                    await page.click("button[data-a-target='passport-login-button']")
                    await asyncio.sleep(4)

                # Check if 2FA is requested
                two_factor_input = page.locator("input[autocomplete='one-time-code']")
                if await two_factor_input.count() > 0:
                    logger.warning("Twitch requested 2FA authentication code!")
                    # Take screenshot for user inspection
                    SCREENSHOT_2FA_PATH.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(SCREENSHOT_2FA_PATH))
                    logger.info(f"2FA screen saved to {SCREENSHOT_2FA_PATH}")

                    code = two_factor_code
                    if not code:
                        logger.info(
                            f"Waiting for 2FA token in {TOKEN_2FA_PATH} (timeout {timeout}s)..."
                        )
                        start_wait = asyncio.get_event_loop().time()
                        while (asyncio.get_event_loop().time() - start_wait) < timeout:
                            if TOKEN_2FA_PATH.exists():
                                code = TOKEN_2FA_PATH.read_text().strip()
                                TOKEN_2FA_PATH.unlink(missing_ok=True)
                                if code:
                                    logger.info("Read 2FA code from token file")
                                    break
                            await asyncio.sleep(2)

                    if not code:
                        logger.error("Timed out waiting for 2FA code.")
                        return False

                    logger.info("Submitting 2FA code...")
                    await two_factor_input.fill(code)
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(5)

                # Wait for navigation/auth-token
                for _ in range(15):
                    cookies = await context.cookies(["https://www.twitch.tv"])
                    if any(c["name"] == "auth-token" for c in cookies):
                        logger.info("Login successful! auth-token captured.")
                        SCREENSHOT_2FA_PATH.unlink(missing_ok=True)
                        TOKEN_2FA_PATH.unlink(missing_ok=True)
                        return self.save_cookies_list(cookies, self.cookies_path)
                    await asyncio.sleep(2)

                logger.error("Login attempt completed but no auth-token was found.")
                return False

            finally:
                await context.close()
