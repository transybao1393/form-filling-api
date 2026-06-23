"""WebSocket connection manager and Redis fan-out."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from api import config, event_bus, job_store, models, template_task_store
from . import snapshots

log = logging.getLogger("ws_service.manager")

MAX_CHANNELS = 20


def _redis_channel_to_logical(redis_channel: str) -> str | None:
    if redis_channel.startswith("fp:user:"):
        return f"user:{redis_channel.removeprefix('fp:user:')}"
    if redis_channel.startswith("fp:job:"):
        return f"job:{redis_channel.removeprefix('fp:job:')}"
    if redis_channel.startswith("fp:template:"):
        return f"template:{redis_channel.removeprefix('fp:template:')}"
    return None


@dataclass
class ClientConnection:
    websocket: WebSocket
    connection_id: str
    user: models.User | None
    channels: set[str] = field(default_factory=set)
    missed_pongs: int = 0


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: dict[str, ClientConnection] = {}
        self._channel_index: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def register(
        self, websocket: WebSocket, user: models.User | None,
    ) -> ClientConnection:
        conn_id = f"conn_{secrets.token_urlsafe(12)}"
        conn = ClientConnection(
            websocket=websocket,
            connection_id=conn_id,
            user=user,
        )
        async with self._lock:
            self._clients[conn_id] = conn
        return conn

    async def unregister(self, conn_id: str) -> None:
        async with self._lock:
            conn = self._clients.pop(conn_id, None)
            if conn is None:
                return
            for ch in conn.channels:
                subs = self._channel_index.get(ch)
                if subs:
                    subs.discard(conn_id)
                    if not subs:
                        del self._channel_index[ch]

    async def can_subscribe(
        self, conn: ClientConnection, channel: str,
    ) -> tuple[bool, str | None]:
        if channel.startswith("user:"):
            try:
                user_id = int(channel[5:])
            except ValueError:
                return False, "Invalid user channel"
            if conn.user is None:
                if config.AUTH_REQUIRED:
                    return False, "Authentication required"
                return True, None
            if conn.user.id != user_id:
                return False, "Forbidden"
            return True, None

        if channel.startswith("job:"):
            job_id = channel[4:]
            meta = job_store.get_meta(job_id)
            if meta is None:
                return False, "Job not found"
            job_team = meta.get("team_id")
            if conn.user is None:
                if config.AUTH_REQUIRED or job_team is not None:
                    return False, "Authentication required"
                return True, None
            if job_team != conn.user.team_id:
                return False, "Forbidden"
            return True, None

        if channel.startswith("template:"):
            task_id = channel[9:]
            meta = template_task_store.get_meta(task_id)
            if meta is None:
                return False, "Template task not found"
            task_user = meta.get("user_id")
            if conn.user is None:
                if config.AUTH_REQUIRED or task_user is not None:
                    return False, "Authentication required"
                return True, None
            if task_user is not None and task_user != conn.user.id:
                return False, "Forbidden"
            return True, None

        return False, "Unknown channel format"

    async def subscribe(
        self, conn: ClientConnection, channels: list[str],
    ) -> list[dict[str, Any]]:
        """Subscribe to channels. Returns list of frames to send (acks + snapshots)."""
        frames: list[dict[str, Any]] = []
        async with self._lock:
            for channel in channels:
                if channel in conn.channels:
                    continue
                if len(conn.channels) >= MAX_CHANNELS:
                    frames.append({
                        "v": 1,
                        "op": "subscribe_error",
                        "channel": channel,
                        "code": 4408,
                        "message": "Max channels exceeded",
                    })
                    continue

                ok, reason = await self.can_subscribe(conn, channel)
                if not ok:
                    frames.append({
                        "v": 1,
                        "op": "subscribe_error",
                        "channel": channel,
                        "code": 4403,
                        "message": reason or "Forbidden",
                    })
                    continue

                conn.channels.add(channel)
                self._channel_index.setdefault(channel, set()).add(conn.connection_id)
                frames.append({"v": 1, "op": "subscribed", "channel": channel})

                snap = snapshots.snapshot_for_channel(channel)
                if snap is not None:
                    frames.append({
                        "v": 1,
                        "op": "snapshot",
                        "channel": channel,
                        "data": snap,
                    })
        return frames

    async def unsubscribe(
        self, conn: ClientConnection, channels: list[str],
    ) -> None:
        async with self._lock:
            for channel in channels:
                if channel not in conn.channels:
                    continue
                conn.channels.discard(channel)
                subs = self._channel_index.get(channel)
                if subs:
                    subs.discard(conn.connection_id)
                    if not subs:
                        del self._channel_index[channel]

    async def dispatch_redis_message(
        self, redis_channel: str, payload: str,
    ) -> None:
        logical = _redis_channel_to_logical(redis_channel)
        if logical is None:
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return

        targets: set[str] = {logical}
        if logical.startswith("user:"):
            kind = data.get("kind")
            rid = data.get("id")
            if kind == "template" and rid:
                targets.add(f"template:{rid}")

        frame = {"v": 1, "op": "event", "data": data}
        async with self._lock:
            conn_ids: set[str] = set()
            for ch in targets:
                conn_ids.update(self._channel_index.get(ch, set()))

        for conn_id in conn_ids:
            conn = self._clients.get(conn_id)
            if conn is None:
                continue
            client_channel = logical
            if logical.startswith("user:"):
                kind = data.get("kind")
                rid = data.get("id")
                if kind == "template" and rid and f"template:{rid}" in conn.channels:
                    client_channel = f"template:{rid}"
                elif logical not in conn.channels:
                    continue
            elif logical not in conn.channels:
                continue

            out = {**frame, "channel": client_channel}
            try:
                await conn.websocket.send_json(out)
            except Exception:
                log.debug("send failed conn=%s", conn_id, exc_info=True)

    async def broadcast_ping(self, ts: int) -> None:
        async with self._lock:
            clients = list(self._clients.values())
        for conn in clients:
            try:
                await conn.websocket.send_json({"v": 1, "op": "ping", "ts": ts})
            except Exception:
                pass

    async def close_all(self, code: int = 1001, reason: str = "going away") -> None:
        async with self._lock:
            clients = list(self._clients.values())
        for conn in clients:
            try:
                await conn.websocket.send_json({"v": 1, "op": "going_away"})
                await conn.websocket.close(code=code, reason=reason)
            except Exception:
                pass


manager = ConnectionManager()


async def redis_listener_loop(stop_event: asyncio.Event) -> None:
    """Background task: PSUBSCRIBE fp:user:*, fp:job:* and fp:template:*."""
    import redis.asyncio as aioredis

    backoff = 1.0
    while not stop_event.is_set():
        try:
            r = aioredis.Redis(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                db=config.REDIS_DATABASE,
                decode_responses=True,
            )
            pubsub = r.pubsub()
            await pubsub.psubscribe("fp:user:*", "fp:job:*", "fp:template:*")
            log.info("redis listener subscribed")
            backoff = 1.0

            while not stop_event.is_set():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message and message.get("type") == "pmessage":
                    ch = message.get("channel") or ""
                    data = message.get("data") or ""
                    await manager.dispatch_redis_message(ch, data)

            await pubsub.unsubscribe()
            await pubsub.aclose()
            await r.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            if stop_event.is_set():
                break
            log.warning("redis listener error, retry in %.1fs", backoff, exc_info=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
