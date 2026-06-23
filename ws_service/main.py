"""Dedicated WebSocket service for real-time job/template status updates.

Run with:
    uvicorn ws_service.main:app --host 0.0.0.0 --port 8001
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from api import config, db as app_db
from ws_service import auth, config as ws_config
from ws_service.manager import manager, redis_listener_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ws_service")

_stop_event: asyncio.Event | None = None
_redis_task: asyncio.Task | None = None
_heartbeat_task: asyncio.Task | None = None


async def _heartbeat_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        await asyncio.sleep(ws_config.HEARTBEAT_SEC)
        if stop_event.is_set():
            break
        ts = int(time.time())
        await manager.broadcast_ping(ts)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _stop_event, _redis_task, _heartbeat_task
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    await app_db.init_models()

    _stop_event = asyncio.Event()
    _redis_task = asyncio.create_task(redis_listener_loop(_stop_event))
    _heartbeat_task = asyncio.create_task(_heartbeat_loop(_stop_event))
    log.info("ws_service started JOBS_DIR=%s", config.JOBS_DIR)

    yield

    _stop_event.set()
    await manager.close_all()
    for task in (_redis_task, _heartbeat_task):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    log.info("ws_service stopped")


app = FastAPI(title="Form Pipeline WebSocket", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ws_config.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    try:
        import redis

        r = redis.Redis(
            host=config.REDIS_HOST,
            port=config.REDIS_PORT,
            db=config.REDIS_DATABASE,
        )
        ok = r.ping()
    except Exception:
        ok = False
    return {
        "status": "ok" if ok else "degraded",
        "redis": bool(ok),
        "connections": manager.client_count,
    }


async def _send_json(ws: WebSocket, payload: dict[str, Any]) -> None:
    await ws.send_json(payload)


@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket) -> None:
    if not auth.origin_allowed(websocket.scope):
        await websocket.close(code=1008, reason="Origin not allowed")
        return

    user = await auth.authenticate_websocket(websocket.scope)
    if user is None and config.AUTH_REQUIRED:
        await websocket.close(code=1008, reason="Authentication required")
        return

    await websocket.accept()
    conn = await manager.register(websocket, user)
    try:
        await _send_json(websocket, {
            "v": ws_config.PROTOCOL_VERSION,
            "op": "hello",
            "connection_id": conn.connection_id,
            "heartbeat_sec": ws_config.HEARTBEAT_SEC,
        })

        while True:
            raw = await websocket.receive_json()
            op = raw.get("op")

            if op == "subscribe":
                channels = raw.get("channels") or []
                if not isinstance(channels, list):
                    channels = []
                frames = await manager.subscribe(conn, [str(c) for c in channels])
                for frame in frames:
                    await _send_json(websocket, frame)

            elif op == "unsubscribe":
                channels = raw.get("channels") or []
                if isinstance(channels, list):
                    await manager.unsubscribe(conn, [str(c) for c in channels])

            elif op == "pong":
                conn.missed_pongs = 0

            else:
                await _send_json(websocket, {
                    "v": 1,
                    "op": "error",
                    "message": f"Unknown op: {op}",
                })

    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("ws connection error conn=%s", conn.connection_id, exc_info=True)
    finally:
        await manager.unregister(conn.connection_id)
