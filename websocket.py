from __future__ import annotations

import json
import asyncio
import logging
from time import time
from contextlib import suppress
from typing import Any, Literal, TYPE_CHECKING

import aiohttp

from exceptions import MinerException, WebsocketClosed
from constants import PING_INTERVAL, PING_TIMEOUT, MAX_WEBSOCKETS, WS_TOPICS_LIMIT
from utils import (
    CHARS_ASCII,
    task_wrapper,
    create_nonce,
    json_minify,
    format_traceback,
    AwaitableValue,
    ExponentialBackoff,
    chunk,
)

if TYPE_CHECKING:
    from collections import abc
    from twitch import Twitch
    from constants import JsonType, WebsocketTopic


WSMsgType = aiohttp.WSMsgType
logger = logging.getLogger("TwitchDrops")
ws_logger = logging.getLogger("TwitchDrops.websocket")


class Websocket:
    def __init__(self, pool: WebsocketPool, index: int):
        self._pool: WebsocketPool = pool
        self._twitch: Twitch = pool._twitch
        self._state_lock = asyncio.Lock()
        # websocket index
        self._idx: int = index
        # current websocket connection
        self._ws: AwaitableValue[aiohttp.ClientWebSocketResponse] = AwaitableValue()
        # set when the websocket needs to be closed or reconnect
        self._closed = asyncio.Event()
        self._reconnect_requested = asyncio.Event()
        # set when the topics changed
        self._topics_changed = asyncio.Event()
        # ping timestamps
        self._next_ping: float = time()
        self._max_pong: float = self._next_ping + PING_TIMEOUT.total_seconds()
        # main task, responsible for receiving messages, sending them, and websocket ping
        self._handle_task: asyncio.Task[None] | None = None
        # topics stuff
        self.topics: dict[str, WebsocketTopic] = {}
        self._submitted: set[WebsocketTopic] = set()
        # notify console/log instead of GUI
        self.set_status("Disconnected")

    @property
    def connected(self) -> bool:
        return self._ws.has_value()

    def wait_until_connected(self):
        return self._ws.wait()

    def set_status(self, status: str | None = None, refresh_topics: bool = False):
        """Logs the websocket status instead of updating a GUI."""
        parts = []
        if status:
            parts.append(f"Status: {status}")
        if refresh_topics:
            parts.append(f"Topics: {len(self.topics)}")
        
        if parts:
            ws_logger.debug(f"Websocket[{self._idx}] " + " | ".join(parts))

    def request_reconnect(self):
        # reset our ping interval, so we send a PING after reconnect right away
        self._next_ping = time()
        self._reconnect_requested.set()

    async def start(self):
        async with self._state_lock:
            self.start_nowait()
            await self.wait_until_connected()

    def start_nowait(self):
        if self._handle_task is None or self._handle_task.done():
            self._handle_task = asyncio.create_task(self._handle())

    async def stop(self, *, remove: bool = False):
        async with self._state_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            ws = self._ws.get_with_default(None)
            if ws is not None:
                self.set_status("Disconnecting")
                await ws.close()
            if self._handle_task is not None:
                with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(self._handle_task, timeout=2)
                self._handle_task = None
            if remove:
                self.topics.clear()
                self._topics_changed.set()
                ws_logger.info(f"Websocket[{self._idx}] removed from pool.")

    def stop_nowait(self, *, remove: bool = False):
        asyncio.create_task(task_wrapper(self.stop)(remove=remove))

    async def _backoff_connect(
        self, ws_url: str, **kwargs
    ) -> abc.AsyncGenerator[aiohttp.ClientWebSocketResponse, None]:
        session = await self._twitch.get_session()
        backoff = ExponentialBackoff(**kwargs)
        proxy = self._twitch.settings.proxy if self._twitch.settings.proxy else None
        consecutive_failures = 0
        max_consecutive = 10
        
        for delay in backoff:
            try:
                # Add longer timeout for initial connection
                connect_timeout = aiohttp.ClientTimeout(sock_connect=30, total=60)
                async with session.ws_connect(
                    ws_url, 
                    proxy=proxy, 
                    timeout=connect_timeout,
                    heartbeat=30  # Send heartbeat every 30 seconds
                ) as websocket:
                    consecutive_failures = 0  # Reset on success
                    yield websocket
                    backoff.reset()
            except (
                asyncio.TimeoutError,
                aiohttp.ClientResponseError,
                aiohttp.ClientConnectionError,
            ) as e:
                consecutive_failures += 1
                
                # If we've failed too many times in a row, wait longer
                if consecutive_failures >= max_consecutive:
                    extended_delay = min(delay * 3, 900)  # Cap at 15 minutes
                    ws_logger.warning(
                        f"Websocket[{self._idx}] {consecutive_failures} consecutive failures. "
                        f"Extended retry in {extended_delay:.1f}s"
                    )
                    await asyncio.sleep(extended_delay)
                    consecutive_failures = 0  # Reset counter
                else:
                    ws_logger.info(
                        f"Websocket[{self._idx}] connection failed ({type(e).__name__}), "
                        f"retrying in {delay:.1f}s (attempt {consecutive_failures})"
                    )
                    await asyncio.sleep(delay)
            except RuntimeError:
                ws_logger.warning(
                    f"Websocket[{self._idx}] exiting connect loop, session is closed."
                )
                break
            except Exception as e:
                ws_logger.error(
                    f"Websocket[{self._idx}] unexpected error: {type(e).__name__}: {e}"
                )
                await asyncio.sleep(delay)

    @task_wrapper(critical=True)
    async def _handle(self):
        # Ensure we're logged in before connecting
        self.set_status("Initializing")
        await self._twitch.wait_until_login()
        self.set_status("Connecting")
        self._closed.clear()
        
        reconnect_count = 0
        max_reconnects = 50
        last_success = time()

        async for websocket in self._backoff_connect("wss://pubsub-edge.twitch.tv/v1", maximum=300):
            now = time()
            
            # Reset counter if last connection lasted > 5 minutes
            if now - last_success > 300:
                reconnect_count = 0
            
            reconnect_count += 1
            
            if reconnect_count > max_reconnects:
                logger.error(f"Websocket[{self._idx}] exceeded max reconnections")
                await asyncio.sleep(1800)  # Wait 10 minutes
                reconnect_count = 0
                last_success = time()  # Reset timer
                continue
            
            self._ws.set(websocket)
            self._reconnect_requested.clear()
            # Force topic subscription on new connection
            self._topics_changed.set()
            self.set_status("Connected")
            
            last_success = time()
            
            try:
                while not self._reconnect_requested.is_set():
                    await self._handle_ping()
                    await self._handle_topics()
                    await self._handle_recv()
            except WebsocketClosed as exc:
                if exc.received:
                    # CHANGED: WARNING -> INFO (This is normal Twitch behavior)
                    ws_logger.info(
                        f"Websocket[{self._idx}] closed by server: {websocket.close_code}"
                    )
                elif self._closed.is_set():
                    ws_logger.info(f"Websocket[{self._idx}] stopped.")
                    self.set_status("Disconnected")
                    return
            except asyncio.CancelledError:
                ws_logger.info(f"Websocket[{self._idx}] task cancelled")
                raise
            except Exception as e:
                ws_logger.exception(f"Exception in Websocket[{self._idx}]: {e}")
            finally:
                self._ws.clear()
                self._submitted.clear()
                self._topics_changed.set()
            
            self.set_status("Reconnecting")
            # CHANGED: WARNING -> INFO
            ws_logger.info(f"Websocket[{self._idx}] reconnecting...")
            await asyncio.sleep(5)

    async def _handle_ping(self):
        now = time()
        if now >= self._next_ping:
            self._next_ping = now + PING_INTERVAL.total_seconds()
            self._max_pong = now + PING_TIMEOUT.total_seconds()
            await self.send({"type": "PING"})
        elif now >= self._max_pong:
            ws_logger.warning(f"Websocket[{self._idx}] PONG timeout, reconnecting...")
            self.request_reconnect()

    async def _handle_topics(self):
        if not self._topics_changed.is_set():
            return
        self._topics_changed.clear()
        self.set_status(refresh_topics=True)
        auth_state = await self._twitch.get_auth()
        current = set(self.topics.values())
        
        removed = self._submitted - current
        if removed:
            # Chunking unlisten requests (Safe size: 10, Delay: 0.1s)
            for topics_chunk in chunk(list(removed), 10):
                topics_list = [str(t) for t in topics_chunk]
                ws_logger.debug(f"Websocket[{self._idx}]: Unlistening from topics: {topics_list}")
                await self.send({"type": "UNLISTEN", "data": {"topics": topics_list, "auth_token": auth_state.access_token}})
                await asyncio.sleep(0.1)
            self._submitted.difference_update(removed)
        
        added = current - self._submitted
        if added:
            # Chunking listen requests (Safe size: 10, Delay: 0.1s)
            for topics_chunk in chunk(list(added), 10):
                topics_list = [str(t) for t in topics_chunk]
                ws_logger.debug(f"Websocket[{self._idx}]: Listening to topics: {topics_list}")
                await self.send({"type": "LISTEN", "data": {"topics": topics_list, "auth_token": auth_state.access_token}})
                await asyncio.sleep(0.1)
            self._submitted.update(added)

    async def _gather_recv(self, messages: list[JsonType], timeout: float = 0.5):
        ws = await self._ws.get()
        deadline = time() + timeout
        
        while time() < deadline:
            try:
                remaining = deadline - time()
                if remaining <= 0:
                    break
                
                try:
                    raw_message: aiohttp.WSMessage = await ws.receive(timeout=remaining)
                except (aiohttp.ClientConnectionResetError, ConnectionResetError, aiohttp.ClientPayloadError):
                    raise WebsocketClosed()
                
                ws_logger.debug(f"Websocket[{self._idx}] received: {raw_message.type}")
                
                if raw_message.type is WSMsgType.TEXT:
                    try:
                        messages.append(json.loads(raw_message.data))
                    except json.JSONDecodeError as e:
                        ws_logger.error(f"Failed to parse websocket message: {e}")
                        continue
                elif raw_message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
                    raise WebsocketClosed(received=True)
                elif raw_message.type is WSMsgType.ERROR:
                    ws_logger.error(f"Websocket[{self._idx}] error: {ws.exception()}")
                    raise WebsocketClosed()
            except asyncio.TimeoutError:
                break  # Normal timeout, exit loop

    def _handle_message(self, message):
        if (topic := self.topics.get(message["data"]["topic"])):
            asyncio.create_task(topic(json.loads(message["data"]["message"])))

    async def _handle_recv(self):
        messages: list[JsonType] = []
        try:
            await self._gather_recv(messages, timeout=0.5)
        except asyncio.TimeoutError:
            pass # No messages received, which is normal
        
        for message in messages:
            msg_type = message["type"]
            if msg_type == "MESSAGE":
                self._handle_message(message)
            elif msg_type == "PONG":
                self._max_pong = self._next_ping
            elif msg_type == "RECONNECT":
                ws_logger.info(f"Websocket[{self._idx}] server requested reconnect.")
                self.request_reconnect()

    def add_topics(self, topics_set: set[WebsocketTopic]):
        changed = False
        while topics_set and len(self.topics) < WS_TOPICS_LIMIT:
            topic = topics_set.pop()
            self.topics[str(topic)] = topic
            changed = True
        if changed:
            self._topics_changed.set()

    def remove_topics(self, topics_set: set[str]):
        existing = topics_set.intersection(self.topics.keys())
        if not existing:
            return
        topics_set.difference_update(existing)
        for topic in existing:
            del self.topics[topic]
        self._topics_changed.set()

    async def send(self, message: JsonType):
        try:
            ws = await self._ws.get()
            if message["type"] != "PING":
                message["nonce"] = create_nonce(CHARS_ASCII, 30)
            
            await ws.send_json(message, dumps=json_minify)
            ws_logger.debug(f"Websocket[{self._idx}] sent: {message}")
        except (aiohttp.ClientConnectionResetError, ConnectionResetError, aiohttp.ClientError):
            # CHANGED: WARNING -> DEBUG (Auto-reconnect handles this, no need to spam logs)
            ws_logger.debug(f"Websocket[{self._idx}] failed to send (Connection Reset). Requesting reconnect.")
            self.request_reconnect()
        except Exception as e:
            ws_logger.error(f"Websocket[{self._idx}] send error: {e}")


class WebsocketPool:
    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._running = asyncio.Event()
        self.websockets: list[Websocket] = []

    @property
    def running(self) -> bool:
        return self._running.is_set()

    def wait_until_connected(self) -> abc.Coroutine[Any, Any, Literal[True]]:
        return self._running.wait()

    async def start(self):
        self._running.set()
        await asyncio.gather(*(ws.start() for ws in self.websockets))

    async def stop(self, *, clear_topics: bool = False):
        self._running.clear()
        await asyncio.gather(*(ws.stop(remove=clear_topics) for ws in self.websockets))

    def add_topics(self, topics: abc.Iterable[WebsocketTopic]):
        topics_set = set(topics)
        if not topics_set:
            return
        topics_set.difference_update(*(ws.topics.values() for ws in self.websockets))
        if not topics_set:
            return
        
        for ws_idx in range(MAX_WEBSOCKETS):
            if ws_idx < len(self.websockets):
                ws = self.websockets[ws_idx]
            else:
                ws = Websocket(self, ws_idx)
                if self.running:
                    ws.start_nowait()
                self.websockets.append(ws)
            
            ws.add_topics(topics_set)
            if not topics_set:
                return
        
        if topics_set:
            raise MinerException("Maximum topics limit has been reached across all websockets")

    def remove_topics(self, topics: abc.Iterable[str]):
        topics_set = set(topics)
        if not topics_set:
            return
        
        for ws in self.websockets:
            ws.remove_topics(topics_set)
        
        recycled_topics: list[WebsocketTopic] = []
        while len(self.websockets) > 0 and sum(len(ws.topics) for ws in self.websockets) <= (len(self.websockets) - 1) * WS_TOPICS_LIMIT:
            ws = self.websockets.pop()
            recycled_topics.extend(ws.topics.values())
            ws.stop_nowait(remove=True)
        
        if recycled_topics:
            self.add_topics(recycled_topics)