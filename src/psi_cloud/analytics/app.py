"""埋点收集服务。原 collector.py 的 FastAPI 改写。

保持不变:POST /api/events 路径、入参键名、CORS 行为、
返回体 {"ok": true}、events 表结构。
变化:ThreadingHTTPServer → uvicorn;写库经 anyio.to_thread 出事件循环。
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from ..shared.config import analytics_settings
from ..shared.db import Database, apply_schema
from ..shared.logging import setup_logging
from .models import EventIn
from .schema import SCHEMA

_LOG = logging.getLogger(__name__)

_INSERT = """
    INSERT INTO events
      (name, page, url, referrer, os, device, lang,
       client_id, session_id, ip, props)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = analytics_settings()
    setup_logging(settings.log_level)
    db = Database(settings.db_path)
    apply_schema(db, SCHEMA)
    _LOG.info("analytics schema applied at %s", settings.db_path)
    app.state.db = db
    try:
        yield
    finally:
        _LOG.info("analytics service shutting down")


def _client_ip(request: Request) -> str:
    """取真实客户端 IP。服务在 Caddy 之后,优先信 X-Forwarded-For 首段。"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def create_app() -> FastAPI:
    settings = analytics_settings()
    app = FastAPI(
        title="psi-cloud analytics",
        description="官网埋点收集。",
        version="0.1.0",
        lifespan=lifespan,
    )
    origins = [o.strip() for o in settings.allow_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    @app.get("/healthz", tags=["ops"], summary="健康检查")
    async def healthz() -> dict[str, object]:
        db: Database = app.state.db
        return {"ok": db.healthy(), "service": "analytics"}

    @app.post("/api/events", tags=["events"], summary="上报埋点事件")
    async def collect(event: EventIn, request: Request) -> dict[str, bool]:
        db: Database = app.state.db
        await db.aexecute(
            _INSERT,
            (
                event.name,
                event.page,
                event.url,
                event.referrer,
                event.os,
                event.device,
                event.lang,
                event.clientId,
                event.sessionId,
                _client_ip(request),
                json.dumps(event.props, ensure_ascii=False),
            ),
        )
        return {"ok": True}

    return app


app = create_app()
