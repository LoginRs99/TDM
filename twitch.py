# twitch.py (Final Optimized Version)

from __future__ import annotations

import json
import asyncio
import logging
import math
import random
from time import time
from pathlib import Path
from copy import deepcopy
from itertools import chain
from functools import partial
from collections import abc, deque, OrderedDict
from datetime import datetime, timedelta, timezone
from contextlib import suppress, asynccontextmanager
from typing import Any, Literal, Final, NoReturn, overload, cast, TYPE_CHECKING

import aiohttp
from yarl import URL
from channel import Channel
from websocket import WebsocketPool
from inventory import DropsCampaign
from discord_notifier import DiscordNotifier

from exceptions import (
    ExitRequest,
    GQLException,
    ReloadRequest,
    LoginException,
    MinerException,
    RequestInvalid,
    CaptchaRequired,
    RequestException,
)
from utils import (
    CHARS_HEX_LOWER,
    chunk,
    timestamp,
    create_nonce,
    task_wrapper,
    RateLimiter,
    AwaitableValue,
    ExponentialBackoff,
)
from constants import (
    CALL,
    MAX_INT,
    DUMP_PATH,
    COOKIES_PATH,
    WORKING_DIR,
    MAX_CHANNELS,
    GQL_QUERIES,
    WATCH_INTERVAL,
    State,
    ClientType,
    PriorityMode,
    WebsocketTopic,
)

if TYPE_CHECKING:
    from utils import Game
    from channel import Stream
    from settings import Settings
    from inventory import TimedDrop
    from constants import ClientInfo, JsonType, GQLOperation

logger = logging.getLogger("TwitchDrops")
gql_logger = logging.getLogger("TwitchDrops.gql")
WATCH_STATS_PATH = Path(WORKING_DIR, "watch_stats.json")


class SkipExtraJsonDecoder(json.JSONDecoder):
    def decode(self, s: str, *args):
        obj, end = self.raw_decode(s)
        return obj


SAFE_LOADS = lambda s: json.loads(s, cls=SkipExtraJsonDecoder)

def add_jitter(base_value: float, jitter_percent: float = 0.1) -> float:
    jitter = base_value * jitter_percent
    return base_value + random.uniform(-jitter, jitter)


class _AuthState:
    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._lock = asyncio.Lock()
        self._logged_in = asyncio.Event()
        self.user_id: int
        self.device_id: str
        self.session_id: str
        self.access_token: str

    def _hasattrs(self, *attrs: str) -> bool:
        return all(hasattr(self, attr) for attr in attrs)

    def _delattrs(self, *attrs: str) -> None:
        for attr in attrs:
            if hasattr(self, attr):
                delattr(self, attr)

    def clear(self) -> None:
        self._delattrs("user_id", "device_id", "session_id", "access_token")
        self._logged_in.clear()

    def headers(self, *, user_agent: str = '', gql: bool = False) -> JsonType:
        client_info: ClientInfo = self._twitch._client_type
        headers = {
            "Accept": "*/*", "Accept-Encoding": "gzip", "Accept-Language": "en-US",
            "Pragma": "no-cache", "Cache-Control": "no-cache", "Client-Id": client_info.CLIENT_ID,
        }
        if user_agent: headers["User-Agent"] = user_agent
        if hasattr(self, "session_id"): headers["Client-Session-Id"] = self.session_id
        if hasattr(self, "device_id"): headers["X-Device-Id"] = self.device_id
        if gql:
            headers["Origin"] = str(client_info.CLIENT_URL)
            headers["Referer"] = str(client_info.CLIENT_URL)
            headers["Authorization"] = f"OAuth {self.access_token}"
        return headers

    async def validate(self):
        async with self._lock:
            await self._validate()

    async def _validate(self):
        if self._logged_in.is_set():
            return

        if not hasattr(self, "session_id"):
            self.session_id = create_nonce(CHARS_HEX_LOWER, 16)
        
        session = await self._twitch.get_session()
        jar = cast(aiohttp.CookieJar, session.cookie_jar)
        client_info: ClientInfo = self._twitch._client_type

        if not hasattr(self, "device_id"):
            cookie = jar.filter_cookies(client_info.CLIENT_URL)
            if "unique_id" not in cookie:
                raise LoginException("Device ID (unique_id) not found in cookie. Please log in again to generate it.")
            self.device_id = cookie["unique_id"].value

        logger.info("Validating session from cookie...")
        cookie = jar.filter_cookies(client_info.CLIENT_URL)
        if "auth-token" not in cookie:
            raise LoginException("Authentication token not found in cookies.jar. Please provide a valid cookie file.")
        
        self.access_token = cookie["auth-token"].value
        
        async with self._twitch.request(
            "GET", "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {self.access_token}"}
        ) as response:
            if response.status == 401:
                raise LoginException("Twitch cookie is invalid or expired. Please provide a new cookies.jar file.")
            
            validate_response = await response.json()
            if validate_response["client_id"] != client_info.CLIENT_ID:
                logger.warning("Cookie client ID mismatch. This might cause issues.")

            self.user_id = int(validate_response["user_id"])
            logger.info(f"Login successful via cookie. User: {validate_response.get('login', 'Unknown')}, User ID: {self.user_id}")
            self._logged_in.set()

    def invalidate(self):
        self._delattrs("access_token")


class Twitch:
    def __init__(self, settings: Settings):
        self.settings: Settings = settings
        self._state: State = State.IDLE
        self._state_change = asyncio.Event()
        self._close_requested = asyncio.Event()
        self.wanted_games: list[Game] = []
        self.inventory: list[DropsCampaign] = []
        self._drops: dict[str, TimedDrop] = {}
        self._campaigns: dict[str, DropsCampaign] = {}
        self._mnt_triggers: deque[datetime] = deque()
        self._qgl_limiter = RateLimiter(capacity=3, window=2)
        self._client_type: ClientInfo = ClientType.ANDROID_APP
        self._session: aiohttp.ClientSession | None = None
        self._auth_state: _AuthState = _AuthState(self)
        self.channels: OrderedDict[int, Channel] = OrderedDict()
        self.watching_channel: AwaitableValue[Channel] = AwaitableValue()
        self._watching_task: asyncio.Task[None] | None = None
        self._watching_restart = asyncio.Event()
        self.websocket = WebsocketPool(self)
        self.discord = DiscordNotifier(self)
        self._mnt_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._last_progress_timestamp: float = 0
        self._watch_stats: JsonType | None = None
        if self.settings.enable_watch_stats:
            self._watch_stats = self._load_watch_stats()
        self._last_validation_time: float = 0
        self._validation_failures: int = 0
        self._current_watch_interval: float = WATCH_INTERVAL.total_seconds()
        self._consecutive_gql_failures: int = 0
        self._last_inventory_fetch: datetime = datetime.now(timezone.utc)
        self._session_created: float = 0
        # Initialize Blacklist
        self._channel_blacklist: dict[int, int] = {}
        
    @property
    def close_requested(self) -> bool:
        return self._close_requested.is_set()

    async def wait_until_closed(self):
        await self._close_requested.wait()

    def prevent_close(self):
        self._close_requested.clear()
        
    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            # Refresh session every 6 hours instead of 12 for better stability
            if time() - self._session_created > 43200:
                logger.info("Refreshing HTTP session (12h maintenance)")
                self._session = None  # Just orphan it, let GC clean up
        
        if self._session is None or self._session.closed:
            cookie_jar = aiohttp.CookieJar()
            if COOKIES_PATH.exists():
                try:
                    cookie_jar.load(COOKIES_PATH)
                except Exception as e:
                    logger.warning(f"Could not load cookies.jar: {e}")
                    cookie_jar.clear()
            
            # Increased timeouts for better reliability
            timeout = aiohttp.ClientTimeout(sock_connect=30, sock_read=60, total=90)
            connector = aiohttp.TCPConnector(
                limit=30,
                limit_per_host=6,
                ttl_dns_cache=300,
                force_close=False,
                enable_cleanup_closed=True,
                keepalive_timeout=60  # Increased from 30
            )
            
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                cookie_jar=cookie_jar,
                headers={"User-Agent": self._client_type.USER_AGENT},
            )
            self._session_created = time()
            logger.info("Created new HTTP session")
        
        return self._session

    async def shutdown(self) -> None:
        start_time = time()
        self.stop_watching()
        tasks_to_cancel = [self._watching_task, self._mnt_task, self._health_task]
        for task in tasks_to_cancel:
            if task:
                task.cancel()
        
        await self.discord.stop()
        await self.websocket.stop(clear_topics=True)
        
        # Only save watch stats if enabled
        if self.settings.enable_watch_stats and self._watch_stats:
            self._save_watch_stats()
            logger.info(f"📊 Watch stats saved: {self._watch_stats['successful_watches']} successful, {self._watch_stats['failed_watches']} failed")
        
        if self._session:
            try:
                cookie_jar = cast(aiohttp.CookieJar, self._session.cookie_jar)
                cookie_jar.save(COOKIES_PATH)
            except (PermissionError, OSError) as e:
                logger.warning(f"Could not save cookies.jar (Permission denied?): {e}")
            
            await self._session.close()
        
        self._drops.clear(); self.channels.clear(); self.inventory.clear()
        self._auth_state.clear(); self.wanted_games.clear(); self._mnt_triggers.clear()
        await asyncio.sleep(start_time + 0.5 - time())

    async def run(self):
        if self.settings.dump:
            with open(DUMP_PATH, 'w', encoding="utf8"): pass
        
        self._health_task = asyncio.create_task(self._health_check_loop())
        await self.discord.start()
        
        try:
            await self._run()
        except (ReloadRequest, ExitRequest):
            pass
        except aiohttp.ContentTypeError as exc:
            raise RequestException("Unexpected content type from Twitch API") from exc

    def wait_until_login(self) -> abc.Coroutine[Any, Any, Literal[True]]:
        return self._auth_state._logged_in.wait()

    def change_state(self, state: State) -> None:
        if self._state is not State.EXIT:
            self._state = state
        self._state_change.set()

    def close(self):
        self.change_state(State.EXIT)
        self._close_requested.set()

    def save(self, *, force: bool = False) -> None:
        self.settings.save(force=force)
        
    def update_healthcheck(self, *, error: bool = False) -> None:
        try:
            with open('healthcheck.timestamp', 'w') as f:
                status = f"{int(time())},{self._consecutive_gql_failures}"
                if error: status += ",ERROR"
                f.write(status)
        except Exception as e:
            logger.debug(f"Failed to update healthcheck: {e}")
        
    def get_balanced_priority(self, channel: Channel) -> float:
        """
        Final Optimized Priority Logic.
        Lower score = higher priority.
        
        Factors:
        1. Config Priority (High/Low)
        2. Urgency (Ending soon)
        3. Feasibility (Can we finish it?)
        4. Lock-in (Are we almost done?)
        """
        if (game := channel.game) is None:
            return float('inf')
        
        active_campaign = self.get_active_campaign(channel)
        if not active_campaign:
            return float('inf')
        
        # Configuration
        pref_weight = self.settings.priority_weight_preference / 100.0
        urgency_weight = self.settings.priority_weight_urgency / 100.0
        urgency_window_hours = self.settings.priority_urgency_window_hours
        
        # --- 1. BASE PRIORITY ---
        if game in self.wanted_games:
            try:
                # If it's in our "wanted" list, score is 0.0 to 1.0 based on position
                base_score = self.wanted_games.index(game) / max(len(self.wanted_games), 1)
            except ValueError:
                base_score = 1.0
        else:
            # Filler games get a score of 2.0 (lowest priority)
            base_score = 2.0
            
        # --- 2. URGENCY SCORE ---
        now = datetime.now(timezone.utc)
        if active_campaign.ends_at:
            time_remaining_hours = (active_campaign.ends_at - now).total_seconds() / 3600
        else:
            time_remaining_hours = 999
        
        if time_remaining_hours <= 0:
            return float('inf') # Expired
            
        # Linear curve: 0h left = 0.0 score (Max Urgent), Window limit = 1.0 score
        urgency_score = min(time_remaining_hours / max(urgency_window_hours, 1), 1.0)
        
        # --- 3. LOCK-IN BONUS (Efficiency Boost) ---
        # If we are > 85% done with a drop, reduce score (increase priority) by 0.5
        completion_bonus = 0.0
        if active_campaign.progress > 0.85:
            completion_bonus = -0.5
            
        # --- 4. FEASIBILITY CHECK ---
        # If drop needs 60 mins but campaign ends in 30 mins, penalize heavily (+5.0)
        remaining_minutes = active_campaign.remaining_minutes
        available_minutes = time_remaining_hours * 60
        
        feasibility_penalty = 5.0 if remaining_minutes > available_minutes else 0.0
        
        # --- FINAL CALCULATION ---
        final_score = (
            (base_score * pref_weight) +
            (urgency_score * urgency_weight) +
            completion_bonus +
            feasibility_penalty
        )
        
        return final_score

    def get_priority(self, channel: Channel) -> int | float:
        if self.settings.priority_mode is PriorityMode.BALANCED:
            return self.get_balanced_priority(channel)
        if (game := channel.game) is None or game not in self.wanted_games:
            return MAX_INT
        return self.wanted_games.index(game)

    def get_smart_campaigns(self) -> list[DropsCampaign]:
        """
        Final Optimized Selection Logic.
        
        Tiers:
        1. Priority Games (from settings)
        2. Urgent Campaigns (Ending < 6h)
        3. Filler (Everything else, sorted by availability)
        """
        now = datetime.now(timezone.utc)
        priority_list = self.settings.priority
        priority_mode = self.settings.priority_mode
        
        # 1. Get ALL potentially valid campaigns
        # Look ahead 7 days to ensure we always have something to do
        all_campaigns = [
            c for c in self.inventory 
            if not c.has_badge_or_emote
            and c.game.name not in self.settings.exclude
            and c.can_earn_within(now + timedelta(days=7))
        ]
        
        if not all_campaigns:
            return []
            
        # If user strictly wants ONLY priority games:
        if priority_mode == PriorityMode.PRIORITY_ONLY:
            return sorted(
                [c for c in all_campaigns if c.game.name in priority_list],
                key=lambda c: priority_list.index(c.game.name)
            )

        # For BALANCED / STANDARD modes:
        
        # A. Priority Campaigns (The games you listed in settings)
        priority_campaigns = [c for c in all_campaigns if c.game.name in priority_list]
        priority_campaigns.sort(key=lambda c: priority_list.index(c.game.name))
        
        # B. Urgent Non-Priority (Ending within 6 hours)
        urgent_campaigns = [
            c for c in all_campaigns 
            if c.game.name not in priority_list 
            and (c.ends_at - now).total_seconds() < (6 * 3600)
            # Simple feasibility check
            and c.remaining_minutes <= (c.ends_at - now).total_seconds() / 60
        ]
        urgent_campaigns.sort(key=lambda c: c.ends_at)
        
        # C. Filler (Everything else that is currently active)
        filler_campaigns = [
            c for c in all_campaigns 
            if c.game.name not in priority_list 
            and c not in urgent_campaigns
            and c.active
        ]
        # Sort filler by availability (easiest to catch first)
        filler_campaigns.sort(key=lambda c: c.availability)
        
        # Combine lists: Priority -> Urgent -> Filler
        final_list = priority_campaigns + urgent_campaigns + filler_campaigns
        
        return final_list

    @staticmethod
    def _viewers_key(channel: Channel) -> int:
        return channel.viewers if channel.viewers is not None else -1

    async def _run(self):
        auth_state = await self.get_auth()
        # --- ADDED: Configuration Summary ---
        logger.info(
            f"⚠️️ Configuration: Mode={self.settings.priority_mode.name} | "
            f"Priority Games={len(self.settings.priority)} | "
            f"Excluded={len(self.settings.exclude)}"
        )
        # ------------------------------------
        await self.websocket.start()
        
        self._watching_task = asyncio.create_task(self._watch_loop())
        
        self.websocket.add_topics([
            WebsocketTopic("User", "Drops", auth_state.user_id, self.process_drops),
            WebsocketTopic("User", "Notifications", auth_state.user_id, self.process_notifications),
        ])
        
        full_cleanup: bool = False
        channels: Final[OrderedDict[int, Channel]] = self.channels
        self.change_state(State.INVENTORY_FETCH)
        
        while True:
            await self._state_change.wait()

            if self._state is State.IDLE:
                # CHANGED: Smart sleep based on next campaign
                next_start = 900  # Default 15 mins
                
                # Check upcoming campaigns
                now = datetime.now(timezone.utc)
                upcoming = [c.starts_at for c in self.inventory if c.upcoming]
                if upcoming:
                    seconds_until = (min(upcoming) - now).total_seconds()
                    if 0 < seconds_until < 900:
                        next_start = seconds_until + 10  # +10s buffer
                        logger.info(f"⏳ Next campaign starts in {int(next_start/60)} min. Sleeping until then.")
                    
                logger.info(f"State: IDLE. Waiting {int(next_start)}s or for event.")
                self.stop_watching()
                self._state_change.clear()
                try:
                    await asyncio.wait_for(self._state_change.wait(), timeout=next_start)
                except asyncio.TimeoutError:
                    logger.info("Idle timeout reached. Proactively re-scanning inventory.")
                    self.change_state(State.INVENTORY_FETCH)
            
            elif self._state is State.INVENTORY_FETCH:
                logger.info("State: INVENTORY_FETCH")
                await self.fetch_inventory()
                if self._state is State.INVENTORY_FETCH:
                    self.save()
                    self.change_state(State.GAMES_UPDATE)

            elif self._state is State.GAMES_UPDATE:
                logger.info("State: GAMES_UPDATE")
                
                # First, claim any ready drops IMMEDIATELY
                claims_made = 0
                for campaign in self.inventory:
                    if not campaign.upcoming:
                        for drop in campaign.drops:
                            if drop.can_claim:
                                if await drop.claim():
                                    self.discord.add_drop(drop)
                                    claims_made += 1
                
                if claims_made > 0:
                    logger.info(f"✅ Claimed {claims_made} drop(s)")
                
                # Use improved campaign selection
                self.wanted_games.clear()
                selected_campaigns = self.get_smart_campaigns()
                
                # Extract unique games from selected campaigns
                self.wanted_games = []
                for campaign in selected_campaigns:
                    if campaign.game not in self.wanted_games:
                        self.wanted_games.append(campaign.game)
                
                if self.wanted_games:
                    logger.info(f"📋 Selected games to farm: {[g.name for g in self.wanted_games[:5]]}")
                    if len(self.wanted_games) > 5:
                        logger.info(f"   ... and {len(self.wanted_games) - 5} more")
                else:
                    logger.info("No active campaigns to farm. Going idle.")
                
                full_cleanup = True
                self.restart_watching()
                self.change_state(State.CHANNELS_CLEANUP)

            elif self._state is State.CHANNELS_CLEANUP:
                logger.info("State: CHANNELS_CLEANUP")
                to_remove = [
                    ch for ch in channels.values() 
                    if full_cleanup or (not ch.acl_based and (ch.offline or ch.game not in self.wanted_games))
                ]
                if to_remove:
                    topics = [WebsocketTopic.as_str("Channel", name, ch.id) for ch in to_remove for name in ("StreamState", "StreamUpdate")]
                    self.websocket.remove_topics(topics)
                    for ch in to_remove: del channels[ch.id]
                
                if self.wanted_games: self.change_state(State.CHANNELS_FETCH)
                else:
                    logger.info("No active campaigns to farm. Going idle.")
                    self.change_state(State.IDLE)

            elif self._state is State.CHANNELS_FETCH:
                logger.info("State: CHANNELS_FETCH")
                new_channels: set[Channel] = set(channels.values())
                channels.clear()
                
                campaigns_to_scan = [c for c in self.inventory if c.game in self.wanted_games and c.can_earn_within(datetime.now(timezone.utc) + timedelta(hours=1))]
                acl_channels = {ch for c in campaigns_to_scan if c.allowed_channels for ch in c.allowed_channels}
                no_acl_games = {c.game for c in campaigns_to_scan if not c.allowed_channels}

                await self.bulk_check_online(acl_channels - new_channels)
                new_channels.update(acl_channels)
                
                for game in no_acl_games:
                    new_channels.update(await self.get_live_streams(game))
                
                ordered = sorted(new_channels, key=self._viewers_key, reverse=True)
                ordered.sort(key=lambda ch: ch.acl_based, reverse=True)
                ordered.sort(key=self.get_priority)
                
                for channel in ordered[:MAX_CHANNELS]: 
                    channels[channel.id] = channel
                
                # Create websocket topics with proper method references
                topics = []
                for channel_id in channels:
                    topics.append(WebsocketTopic("Channel", "StreamState", channel_id, self.process_stream_state))
                    topics.append(WebsocketTopic("Channel", "StreamUpdate", channel_id, self.process_stream_update))
                self.websocket.add_topics(topics)
                
                if (wc := self.watching_channel.get_with_default(None)) and (new_wc := channels.get(wc.id)):
                    if self.can_watch(new_wc): self.watch(new_wc, update_status=False)
                    else: self.stop_watching()
                
                self.change_state(State.CHANNEL_SWITCH)

            elif self._state is State.CHANNEL_SWITCH:
                logger.info("State: CHANNEL_SWITCH")
                new_watching = next((ch for ch in sorted(channels.values(), key=self.get_priority) if self.should_switch(ch)), None)
                
                if new_watching:
                    await asyncio.sleep(random.uniform(2, 8))
                    self.watch(new_watching)
                    self._state_change.clear()
                elif (wc := self.watching_channel.get_with_default(None)) and self.can_watch(wc):
                    logger.info(f"Continuing to watch {wc.name}")
                    self._state_change.clear()
                else:
                    logger.info("No suitable channel to watch. Going idle.")
                    self.change_state(State.IDLE)
            
            elif self._state is State.EXIT:
                logger.info("State: EXIT. Shutting down.")
                break

    async def _watch_sleep(self, delay: float) -> None:
        self._watching_restart.clear()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._watching_restart.wait(), timeout=delay)

    @task_wrapper(critical=True)
    async def _watch_loop(self) -> NoReturn:
        while True:
            channel: Channel = await self.watching_channel.get()
            if not channel.online:
                self.stop_watching()
                self.change_state(State.CHANNEL_SWITCH)
                continue

            # Check for stale stream
            if self._last_progress_timestamp > 0:
                timeout = getattr(self.settings, 'stale_stream_timeout_minutes', 5) * 60
                if time() - self._last_progress_timestamp > timeout:
                    logger.warning(f"❌ No drop progress on '{channel.name}' for {timeout/60} mins. Forcing switch.")
                    self._last_progress_timestamp = 0
                    self.change_state(State.CHANNEL_SWITCH)
                    await asyncio.sleep(5)
                    continue

            interval = self.get_adaptive_watch_interval()
            
            # Send watch with fallback
            watch_success = await channel.send_watch()
            self._update_watch_stats(watch_success, channel)

            if watch_success:
                logger.debug(f"✔ Watch sent to {channel.name}")
                # Clear blacklist on success
                if channel.id in self._channel_blacklist:
                    del self._channel_blacklist[channel.id]
                    logger.info(f"✅ Removed {channel.name} from blacklist (now working)")
            else:
                logger.warning(f"⚠️ Watch failed on {channel.name}")
                
                # Blacklist logic
                self._channel_blacklist[channel.id] = self._channel_blacklist.get(channel.id, 0) + 1
                fail_count = self._channel_blacklist[channel.id]
                
                if fail_count >= 5:
                    logger.error(
                        f"⏭ Blacklisting {channel.name} after {fail_count} failures. "
                        f"Switching to different channel..."
                    )
                    self._last_progress_timestamp = 0
                    self.change_state(State.CHANNEL_SWITCH)
                    await asyncio.sleep(2)
                    continue
              
            last_sent = time()
            
            await asyncio.sleep(add_jitter(20, 0.2))

            # Check progress
            try:
                if not channel or not channel.id:
                    continue

                context = await self.gql_request(
                    GQL_QUERIES["CurrentDrop"].with_variables({"channelID": str(channel.id)})
                )
                if (drop_data := context["data"]["currentUser"]["dropCurrentSession"]) and \
                   (gql_drop := self._drops.get(drop_data["dropID"])) and \
                   gql_drop.can_earn(channel):
                    
                    old_minutes = gql_drop.current_minutes
                    gql_drop.update_minutes(drop_data["currentMinutesWatched"])
                    new_minutes = gql_drop.current_minutes
                    
                    if new_minutes > old_minutes:
                        self._last_progress_timestamp = time()
                        progress_gain = new_minutes - old_minutes
                        
                        # Track watch stats if enabled
                        if self.settings.enable_watch_stats and self._watch_stats:
                            self._watch_stats['total_watch_time_minutes'] += progress_gain
                            
                            if len(self._watch_stats['progress_intervals']) > 0:
                                last_time = self._watch_stats['progress_intervals'][-1]
                                interval_seconds = time() - last_time
                                logger.debug(f"Progress interval: {interval_seconds:.0f}s")
                            
                            self._watch_stats['progress_intervals'].append(time())
                            if len(self._watch_stats['progress_intervals']) > 100:
                                self._watch_stats['progress_intervals'] = self._watch_stats['progress_intervals'][-100:]
                        
                        logger.info(f"📊 Progress: {gql_drop.name} -> {new_minutes}/{gql_drop.required_minutes} min (+{progress_gain})")
                    
                    # Validation check
                    time_since_progress = time() - self._last_progress_timestamp if self._last_progress_timestamp > 0 else 0
                    if time_since_progress > 120 and (time() - self._last_validation_time) > 300:
                        self._last_validation_time = time()
                        logger.info(f"⏱️ No progress for {time_since_progress:.0f}s, running validation...")
                        
                        if not await self.validate_watch_progress(channel):
                            self._validation_failures += 1
                            logger.error(f"❌ Validation failed {self._validation_failures} time(s)")
                            
                            if self._validation_failures >= 3:
                                logger.error(f"🔧 Multiple validation failures, switching channel...")
                                self._validation_failures = 0
                                self._last_progress_timestamp = 0
                                self.change_state(State.CHANNEL_SWITCH)
                                continue
                        
                elif (active_campaign := self.get_active_campaign(channel)):
                    self._last_progress_timestamp = time()
                    active_campaign.bump_minutes(channel)
                    logger.debug(f"⏱️ Bumped minutes for {active_campaign.name}")
                    
            except GQLException as e:
                logger.warning(f"GQL error during progress check: {e}")
                if (active_campaign := self.get_active_campaign(channel)):
                    self._last_progress_timestamp = time()
                    active_campaign.bump_minutes(channel)

            await self._watch_sleep(interval - min(time() - last_sent, interval))

    @task_wrapper(critical=True)
    async def _health_check_loop(self) -> NoReturn:
        while True:
            self.update_healthcheck()
            await asyncio.sleep(30)

    @task_wrapper(critical=True)
    async def _maintenance_task(self) -> None:
        base_minutes = getattr(self.settings, 'maintenance_interval_minutes', 10)
        next_period = datetime.now(timezone.utc) + timedelta(minutes=add_jitter(base_minutes, 0.15))
        
        while (now := datetime.now(timezone.utc)) < next_period:
            next_trigger = next_period
            if self._mnt_triggers and self._mnt_triggers[0] < next_trigger:
                next_trigger = self._mnt_triggers.popleft()
            
            await asyncio.sleep((next_trigger - now).total_seconds())
            
            if datetime.now(timezone.utc) < next_period:
                self.change_state(State.CHANNELS_CLEANUP)
        
        self.change_state(State.INVENTORY_FETCH)

    def can_watch(self, channel: Channel) -> bool:
        # Check blacklist
        if channel.id in self._channel_blacklist:
            if self._channel_blacklist[channel.id] >= 3:
                logger.debug(f"⏭ Skipping blacklisted channel {channel.name} (failures)")
                return False

        if not channel.online or not channel.drops_enabled: return False
        return any(c.can_earn(channel) for c in self.inventory if c.game in self.wanted_games or not c.has_badge_or_emote)

    def should_switch(self, channel: Channel) -> bool:
        # ADD: candidate must be watchable first
        if not self.can_watch(channel):
            return False

        wc = self.watching_channel.get_with_default(None)
        # MODIFIED: also return True if current channel is no longer watchable
        if not wc or not self.can_watch(wc):
            return True

        # Keep your existing lock-in logic unchanged below this line
        current_campaign = self.get_active_campaign(wc)
        if current_campaign:
            first_drop = current_campaign.first_drop
            if first_drop and first_drop.can_claim:
                logger.debug(f"🔒 Lock-in: Drop ready to claim on {wc.name}")
                return False
            if first_drop and 0 < first_drop.remaining_minutes <= 5:
                logger.debug(f"🔒 Lock-in: Only {first_drop.remaining_minutes} min left")
                return False

        p_candidate = self.get_priority(channel)
        p_current = self.get_priority(wc)
        return p_candidate < p_current or (
            p_candidate == p_current and channel.acl_based and not wc.acl_based
        )

    def watch(self, channel: Channel, *, update_status: bool = True):
        self.watching_channel.set(channel)
        if update_status:
            logger.info(f"Now watching: {channel.name} for game {channel.game.name if channel.game else 'N/A'}")

    def stop_watching(self):
        self.watching_channel.clear()

    def restart_watching(self):
        self._watching_restart.set()

    def on_channel_update(self, channel: Channel, stream_before: Stream | None, stream_after: Stream | None):
        if stream_before is None and stream_after is not None:
            if channel.drops_enabled and channel.game in self.wanted_games:
                logger.info(f"⚡ Priority channel {channel.name} came online with drops for {channel.game.name}")
                if (datetime.now(timezone.utc) - self._last_inventory_fetch).total_seconds() > 30:
                    logger.info("Quick inventory refresh triggered by priority channel")
                    asyncio.create_task(self._quick_inventory_check())
            
            if self.should_switch(channel):
                logger.info(f"{channel.name} came online and is a high priority. Switching.")
                self.watch(channel)
        elif stream_before is not None and self.watching_channel.get_with_default(None) == channel and not self.can_watch(channel):
            logger.info(f"No longer able to farm on {channel.name}. Finding new channel.")
            self.change_state(State.CHANNEL_SWITCH)

    @task_wrapper
    async def process_stream_state(self, channel_id: int, message: JsonType):
        if (channel := self.channels.get(channel_id)):
            if message["type"] == "stream-down": channel.set_offline()
            elif message["type"] == "stream-up": channel.check_online()

    @task_wrapper
    async def process_stream_update(self, channel_id: int, message: JsonType):
        if (channel := self.channels.get(channel_id)):
            channel.check_online()

    @task_wrapper
    async def process_drops(self, user_id: int, message: JsonType):
        msg_type, data = message["type"], message["data"]
        if msg_type not in ("drop-progress", "drop-claim"): return
        
        if not (drop := self._drops.get(data["drop_id"])): return
        
        if msg_type == "drop-claim":
            drop.update_claim(data["drop_instance_id"])
            if await drop.claim(): self.discord.add_drop(drop)
            
            await asyncio.sleep(add_jitter(4, 0.25))
            if drop.campaign.can_earn(self.watching_channel.get_with_default(None)): self.restart_watching()
            else: self.change_state(State.INVENTORY_FETCH)
        
        elif msg_type == "drop-progress" and drop.can_earn(self.watching_channel.get_with_default(None)):
            self._last_progress_timestamp = time()
            drop.update_minutes(data["current_progress_min"])

    @task_wrapper
    async def process_notifications(self, user_id: int, message: JsonType):
        # Check if the message is a notification creation event
        if message.get("type") == "create-notification":
            # Safely extract the inner notification data
            data = message.get("data", {}).get("notification", {})
            
            # Define all notification types that should trigger an inventory refresh
            trigger_types = (
                "user_drop_reward_reminder_notification",          # Standard drop claim
                "quests_viewer_reward_campaign_earned_emote",      # Emote rewards
                "completed_campaign_mass_entitlement_notification" # Mass events
            )

            # Check if the notification type matches our list
            if data.get("type") in trigger_types:
                logger.info(f"🔔 Notification received: {data.get('type')}. Syncing inventory.")
                self.change_state(State.INVENTORY_FETCH)
                
                # Ack/Delete the notification to clean up the UI
                if notification_id := data.get("id"):
                    await self.gql_request(
                        GQL_QUERIES["NotificationsDelete"].with_variables({
                            "input": {"id": notification_id}
                        })
                    )

    async def get_auth(self) -> _AuthState:
        await self._auth_state.validate()
        return self._auth_state

    @asynccontextmanager
    async def request(self, method: str, url: URL | str, **kwargs) -> abc.AsyncIterator[aiohttp.ClientResponse]:
        session = await self.get_session()
        if self.settings.proxy and "proxy" not in kwargs:
            kwargs["proxy"] = self.settings.proxy
        
        backoff = ExponentialBackoff(maximum=180)
        for delay in backoff:
            if self.close_requested:
                raise ExitRequest()
            
            if session.closed:
                session = await self.get_session()

            response = None
            try:
                response = await session.request(method, url, **kwargs)
                if response.status < 500:
                    try:
                        yield response
                        return
                    finally:
                        if response and not response.closed:
                            response.close()
                else:
                    logger.warning(f"API Error {response.status}. Retrying in {delay:.1f}s.")
                    if response:
                        response.close()
            except asyncio.CancelledError:
                if response and not response.closed:
                    response.close()
                raise
            except RuntimeError as e:
                if "Session is closed" in str(e):
                    logger.warning("Session was closed externally. Refreshing...")
                    self._session = None
                    continue
                logger.error(f"Unexpected error in request: {e}")
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"Connection error ({type(e).__name__}). Retrying in {delay:.1f}s.")
                if response and not response.closed:
                    response.close()
            except Exception as e:
                logger.error(f"Unexpected error in request: {e}")
                if response and not response.closed:
                    response.close()
                raise
            
            await asyncio.sleep(delay)
        
        raise RequestException(f"Request to {url} failed after multiple retries.")

    @overload
    async def gql_request(self, ops: GQLOperation) -> JsonType: ...
    @overload
    async def gql_request(self, ops: list[GQLOperation]) -> list[JsonType]: ...
    async def gql_request(self, ops: GQLOperation | list[GQLOperation]) -> JsonType | list[JsonType]:
        backoff = ExponentialBackoff(maximum=60)
        single_retry = True
        
        for delay in backoff:
            try:
                async with self._qgl_limiter:
                    auth = await self.get_auth()
                    async with self.request(
                        "POST", 
                        "https://gql.twitch.tv/gql", 
                        json=ops, 
                        headers=auth.headers(gql=True)
                    ) as resp:
                        try:
                            json_resp = await asyncio.wait_for(resp.json(), timeout=30)
                        except asyncio.TimeoutError:
                            logger.error("Timeout while parsing JSON response")
                            await asyncio.sleep(delay)
                            continue
                
                resp_list = json_resp if isinstance(json_resp, list) else [json_resp]
                retry = False
                
                for item in resp_list:
                    if "errors" in item:
                        msg = str(item["errors"]).lower()
                        if single_retry and ("service error" in msg or "persistedquerynotfound" in msg):
                            single_retry = False
                            retry = True
                            break
                        if "service timeout" in msg or "service unavailable" in msg:
                            retry = True
                            break
                        if "unauthorized" in msg or "forbidden" in msg:
                            raise GQLException(item['errors'])
                        if "persistedquerynotfound" not in msg:
                            logger.warning(f"GQL error (will retry): {item['errors']}")
                        retry = True
                        break
                
                if not retry:
                    self._consecutive_gql_failures = 0
                    return json_resp
                    
                if retry:
                    await asyncio.sleep(0.1)  # 100ms cooldown
                    
            except (RequestException, asyncio.TimeoutError) as e:
                logger.warning(f"GQL request failed: {e}. Retrying in {delay:.1f}s")
            except GQLException:
                raise
            except Exception as e:
                logger.error(f"Unexpected error in GQL request: {e}")
                await asyncio.sleep(delay)
        
        raise GQLException("GQL request failed after multiple retries.")

    async def fetch_inventory(self):
        logger.info("Fetching inventory and campaigns...")
        
        if self._channel_blacklist:
            logger.info(f"🔧 Clearing {len(self._channel_blacklist)} blacklisted channels")
            self._channel_blacklist.clear()

        try:
            inv_resp, camp_resp = await asyncio.gather(
                self.gql_request(GQL_QUERIES["Inventory"]),
                self.gql_request(GQL_QUERIES["Campaigns"])
            )
            inventory = inv_resp["data"]["currentUser"]["inventory"]
            claimed_benefits = {b["id"]: timestamp(b["lastAwardedAt"]) for b in inventory["gameEventDrops"]}
            inventory_data = {c["id"]: c for c in inventory["dropCampaignsInProgress"] or []}
            available_campaigns = {c["id"]: c for c in camp_resp["data"]["currentUser"]["dropCampaigns"] or [] if c["status"] in ("ACTIVE", "UPCOMING")}
            
            details_chunks = await asyncio.gather(*[self.fetch_campaigns(chunk) for chunk in chunk(available_campaigns.items(), 20)])
            for chunk_data in details_chunks:
                inventory_data = self._merge_data(inventory_data, chunk_data)
                
            if self.settings.dump:
                with open(DUMP_PATH, 'a', encoding="utf8") as file:
                    dump_data: JsonType = deepcopy(inventory_data)
                    for campaign_data in dump_data.values():
                        if (
                            campaign_data.get("allow", {})
                            and campaign_data["allow"].get("isEnabled", True)
                            and campaign_data["allow"].get("channels")
                        ):
                            campaign_data["allow"]["channels"] = (
                                f"{len(campaign_data['allow']['channels'])} csatorna"
                            )
                        for drop_data in campaign_data.get("timeBasedDrops", []):
                            if "self" in drop_data and drop_data["self"].get("dropInstanceID"):
                                drop_data["self"]["dropInstanceID"] = "..."
                    
                    json.dump(dump_data, file, indent=4, sort_keys=True)
                    file.write("\n\n")
                    json.dump(inventory["gameEventDrops"], file, indent=4, sort_keys=True, default=str)

            campaigns = [DropsCampaign(self, data, claimed_benefits) for data in inventory_data.values() if data.get("game")]
            campaigns.sort(key=lambda c: (not c.eligible, not c.active, c.upcoming and c.starts_at or c.ends_at))

            self._drops.clear(); self.inventory.clear(); self._mnt_triggers.clear()
            self._campaigns.clear()
            
            now, next_hour = datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(hours=12)
            for campaign in campaigns:
                self._drops.update({drop.id: drop for drop in campaign.drops})
                if campaign.can_earn_within(next_hour): self._mnt_triggers.extend(sorted(campaign.time_triggers))
                self.inventory.append(campaign)
                self._campaigns[campaign.id] = campaign
            
            while self._mnt_triggers and self._mnt_triggers[0] <= now: self._mnt_triggers.popleft()
            
            self._consecutive_gql_failures = 0
            self.update_healthcheck()
            
            if self._mnt_task: self._mnt_task.cancel()
            self._mnt_task = asyncio.create_task(self._maintenance_task())
            
        except (GQLException, RequestException) as e:
            self._consecutive_gql_failures += 1
            logger.error(f"Failed to fetch inventory (attempt {self._consecutive_gql_failures}): {e}")
            self.update_healthcheck(error=True)
            if self._consecutive_gql_failures >= 5:
                logger.error("Too many GQL failures. Going idle for maintenance cycle.")
                self._consecutive_gql_failures = 0
                self.change_state(State.IDLE)
            else:
                retry_delay = min(15 * (2 ** self._consecutive_gql_failures), 900)
                logger.warning(f"Retrying inventory fetch in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                self.change_state(State.INVENTORY_FETCH)
                
    async def _quick_inventory_check(self):
        try:
            logger.info("Running quick inventory check...")
            inv_resp = await self.gql_request(GQL_QUERIES["Inventory"])
            inventory = inv_resp["data"]["currentUser"]["inventory"]
            inventory_data = {c["id"]: c for c in inventory["dropCampaignsInProgress"] or []}
            
            for campaign_id, data in inventory_data.items():
                if campaign_id in self._campaigns and data.get("game"):
                    campaign = self._campaigns[campaign_id]
                    for drop_data in data.get("timeBasedDrops", []):
                        if drop_id := drop_data.get("id"):
                            if drop := campaign.timed_drops.get(drop_id):
                                if "self" in drop_data:
                                    old_minutes = drop.real_current_minutes
                                    new_minutes = drop_data["self"]["currentMinutesWatched"]
                                    if new_minutes != old_minutes:
                                        logger.info(f"📊 Progress update: {drop.name} {old_minutes}→{new_minutes}/{drop.required_minutes}min")
                                        drop.real_current_minutes = new_minutes
                                        drop.is_claimed = drop_data["self"]["isClaimed"]
            
            logger.info("Quick inventory check completed")
            self._last_inventory_fetch = datetime.now(timezone.utc)
        except (GQLException, RequestException) as e:
            logger.warning(f"Quick inventory check failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in quick inventory check: {e}")

    async def fetch_campaigns(self, campaigns_chunk: list[tuple[str, JsonType]]) -> dict[str, JsonType]:
        ids, auth = [c[0] for c in campaigns_chunk], await self.get_auth()
        ops = [GQL_QUERIES["CampaignDetails"].with_variables({"channelLogin": str(auth.user_id), "dropID": cid}) for cid in ids]
        details = await self.gql_request(ops)
        fetched_data = {(d["data"]["user"]["dropCampaign"]["id"]): d["data"]["user"]["dropCampaign"] for d in details}
        return self._merge_data(dict(campaigns_chunk), fetched_data)

    def _merge_data(self, primary: JsonType, secondary: JsonType) -> JsonType:
        merged = primary.copy()
        for k, v in secondary.items():
            if k not in merged: merged[k] = v
            elif isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = self._merge_data(merged[k], v)
        return merged

    def get_active_campaign(self, channel: Channel | None = None) -> DropsCampaign | None:
        wc = self.watching_channel.get_with_default(channel)
        if not wc or not wc.id: return None
        campaigns = [c for c in self.inventory if c.can_earn(wc)]
        return min(campaigns, key=lambda c: c.remaining_minutes) if campaigns else None
        
    def _load_watch_stats(self) -> JsonType:
        default_stats = {
            'successful_watches': 0,
            'failed_watches': 0,
            'drops_earned': 0,
            'total_watch_time_minutes': 0,
            'progress_intervals': [],
            'channel_performance': {},
            'last_updated': datetime.now(timezone.utc).isoformat()
        }
        try:
            if WATCH_STATS_PATH.exists():
                with open(WATCH_STATS_PATH, 'r', encoding='utf8') as f:
                    loaded = json.load(f)
                    return {**default_stats, **loaded}
        except Exception as e:
            logger.debug(f"Could not load watch stats: {e}")
        return default_stats
    
    def _save_watch_stats(self) -> None:
        try:
            self._watch_stats['last_updated'] = datetime.now(timezone.utc).isoformat()
            with open(WATCH_STATS_PATH, 'w', encoding='utf8') as f:
                json.dump(self._watch_stats, f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save watch stats: {e}")
    
    def _update_watch_stats(self, success: bool, channel: Channel) -> None:
        if not self.settings.enable_watch_stats or not self._watch_stats:
            return
            
        if success:
            self._watch_stats['successful_watches'] += 1
        else:
            self._watch_stats['failed_watches'] += 1
        
        channel_key = f"{channel.name}"
        if channel_key not in self._watch_stats['channel_performance']:
            self._watch_stats['channel_performance'][channel_key] = {
                'successful': 0,
                'failed': 0,
                'last_watch': None
            }
        
        if success:
            self._watch_stats['channel_performance'][channel_key]['successful'] += 1
        else:
            self._watch_stats['channel_performance'][channel_key]['failed'] += 1
        
        self._watch_stats['channel_performance'][channel_key]['last_watch'] = datetime.now(timezone.utc).isoformat()
        
        total = self._watch_stats['successful_watches'] + self._watch_stats['failed_watches']
        if total % 10 == 0:
            self._save_watch_stats()


    def get_adaptive_watch_interval(self) -> float:
        time_since_progress = time() - self._last_progress_timestamp if self._last_progress_timestamp > 0 else 0
        if time_since_progress < 90:
            interval = WATCH_INTERVAL.total_seconds()
        elif time_since_progress < 150:
            interval = 50
            logger.debug(f"Adaptive interval: Using 50s (no progress for {time_since_progress:.0f}s)")
        else:
            interval = 40
            logger.info(f"⚠️ Adaptive interval: Using 40s (no progress for {time_since_progress:.0f}s)")
        self._current_watch_interval = interval
        return add_jitter(interval, 0.15)

    async def validate_watch_progress(self, channel: Channel) -> bool:
        # FIX: Guard clause for invalid channel
        if not channel or not channel.id:
            return True

        campaign = self.get_active_campaign(channel)
        if not campaign or not campaign.first_drop:
            return True
        
        initial_minutes = campaign.first_drop.current_minutes
        logger.info(f"🔍 Validating watch progress on {channel.name} (current: {initial_minutes} min)...")
        
        for i in range(2):
            success = await channel.send_watch()
            if not success:
                logger.warning(f"Watch request failed during validation (attempt {i+1}/2)")
            await asyncio.sleep(60)
        
        try:
            context = await self.gql_request(
                GQL_QUERIES["CurrentDrop"].with_variables({"channelID": str(channel.id)})
            )
            if (drop_data := context["data"]["currentUser"]["dropCurrentSession"]) and \
               (gql_drop := self._drops.get(drop_data["dropID"])) and \
               gql_drop.can_earn(channel):
                new_minutes = drop_data["currentMinutesWatched"]
                progress_gained = new_minutes - initial_minutes
                
                if progress_gained > 0:
                    logger.info(f"✅ Validation successful: +{progress_gained} min progress on {channel.name}")
                    self._validation_failures = 0
                    return True
                else:
                    logger.warning(f"❌ Validation failed: No progress gained on {channel.name}")
                    return False
        except GQLException as e:
            logger.warning(f"Validation GQL failed: {e}")
            return True
        
        return False

    async def get_live_streams(self, game: Game, limit: int = 20) -> list[Channel]:
        if not game.slug:
            logger.warning(f"Skipping directory fetch for {game.name}: No slug available.")
            return []
            
        try:
            resp = await self.gql_request(GQL_QUERIES["GameDirectory"].with_variables({"limit": limit, "slug": game.slug, "options": {"systemFilters": ["DROPS_ENABLED"]}}))
            if game_data := resp["data"].get("game"):
                return [Channel.from_directory(self, e["node"], drops_enabled=True) for e in game_data["streams"]["edges"] if e["node"]["broadcaster"]]
        except (GQLException, MinerException) as e:
            logger.error(f"Could not fetch streams for {game.name}: {e}")
        return []

    async def bulk_check_online(self, channels: abc.Iterable[Channel]):
        ch_list = list(channels)
        if not ch_list: return
        
        # Filter out channels with invalid IDs to prevent GQL errors
        ch_list = [ch for ch in ch_list if ch.id is not None]
        if not ch_list:
            logger.warning("No valid channels to check online status")
            return
        
        stream_ops = [c.stream_gql for c in ch_list]
        stream_chunks = await asyncio.gather(*[self.gql_request(chunk) for chunk in chunk(stream_ops, 20)])
        streams_map = {}
        for r in stream_chunks:
            for d in r:
                if d.get("data") and (u := d["data"].get("user")) and u.get("id"):
                    streams_map[int(u["id"])] = u

        acl_available_drops_map: dict[int, list[JsonType]] = {}
        
        if self.settings.available_drops_check:
            # ADDED: Performance warning
            if len(streams_map) > 20:
                logger.info(f"⚠️ 'available_drops_check' is on. scanning {len(streams_map)} channels (this may take a moment)...")
            drop_ops = []
            for cid, data in streams_map.items():
                if data.get("stream") and cid is not None:
                    drop_ops.append(
                        GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(cid)}) 
                    )
            
            if drop_ops:
                drop_chunks = await asyncio.gather(*[self.gql_request(chunk) for chunk in chunk(drop_ops, 20)])
                for r in drop_chunks:
                    for d in r:
                        # Defensive check: ensure 'channel' key exists in response
                        if d.get("data") and (c := d["data"].get("channel")) and c.get("id"):
                            acl_available_drops_map[int(c["id"])] = c["viewerDropCampaigns"] or []

        for ch in ch_list:
            if data := streams_map.get(ch.id):
                ch.external_update(data, acl_available_drops_map.get(ch.id, []))