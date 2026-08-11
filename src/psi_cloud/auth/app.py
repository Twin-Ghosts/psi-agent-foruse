"""认证服务 FastAPI 应用。

业务逻辑在 service.py（从 psi-agent-auth 移植），本文件只负责装配：建库、
装 provider、挂路由、把 ServiceError 翻成契约错误体。

三件容易出错、这里刻意写明的事：

1. **前缀 `/api/auth` 来自契约**，不是随手加的。客户端配的是 base URL，
   路径由契约决定；改前缀等于改契约。
2. **测试钩子默认关闭。** 它会回显验证码，公网可达即等于验证码公开。
   只在 `AUTH_TEST_HOOKS=true` 时挂载 —— 服务器验收第 2 条验的就是它 404。
3. **sweep 定时任务。** send_quota / email_codes / sessions 的过期数据没有
   TTL 兜底，不清会静默积垢。写入路径顺手清一次，这里再加低频全表清理。
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..shared.logging import setup_logging
from . import providers_core as providers
from . import service
from .routes import error_body, router
from .store import Store

_LOG = logging.getLogger(__name__)

PREFIX = "/api/auth"


def _flag(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in (
        "1", "true", "yes", "on")


def _channel(name: str, real: str) -> bool:
    """该通道是否走真实供应商。

    口径以 `.env.example` / `docker-compose.yml` 暴露的
    `AUTH_EMAIL_PROVIDER` / `AUTH_SMS_PROVIDER` 为准（值为 `resend` /
    `aliyun` 即真实，`mock` 即假）。`AUTH_USE_REAL_PROVIDERS` 保留为一次性
    打开两个通道的总开关，仅为兼容既有部署脚本。

    为什么不能只认总开关：配置面文档里从来没有它，compose 也没往容器里传，
    照 .env.example 把 `AUTH_EMAIL_PROVIDER=resend` 填好之后通道仍然是 mock
    —— 而日志只会说「MOCK PROVIDER ACTIVE」，看着像没配，实际是配了没生效。
    """
    return (os.environ.get(name, "").strip().lower() == real
            or _flag("AUTH_USE_REAL_PROVIDERS"))


def _build_provider() -> Any:
    """按通道装配。真实通道凭据不全只告警、不回落 mock。

    不回落是有意的：静默回落会让「以为发出去了」成为默认失败模式 —— 用户等
    一封永远不会到的邮件，而服务端一切正常。宁可该通道报错。
    """
    real_email = _channel("AUTH_EMAIL_PROVIDER", "resend")
    real_phone = _channel("AUTH_SMS_PROVIDER", "aliyun")

    if not real_email and not real_phone:
        _LOG.warning("MOCK PROVIDER ACTIVE —— 不发真短信/邮件，生产必须启用真实通道")
        return providers.MockProvider()

    from .real_providers import PnvsProvider, ResendProvider, RoutingProvider

    prov = RoutingProvider(
        email=ResendProvider() if real_email else providers.MockProvider(),
        phone=PnvsProvider() if real_phone else providers.MockProvider(),
    )
    for chan, is_real in (("email", real_email), ("phone", real_phone)):
        sub = getattr(prov, chan, None)
        if is_real and sub is not None and not getattr(
                sub, "ready", lambda: True)():
            _LOG.warning("%s 通道凭据不全，该通道会失败（不静默回落到 mock）", chan)
        if not is_real:
            _LOG.warning("%s 通道仍是 mock —— 不发真信", chan)
    return prov


async def _sweeper(svc: Any, interval: int) -> None:
    """低频清理过期数据。异常不能让它死掉 —— 清理停了不报错，只会积垢。"""
    while True:
        await anyio.sleep(interval)
        with suppress(Exception):
            await svc.sweep()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging(os.environ.get("AUTH_LOG_LEVEL", "INFO"))

    db_path = os.environ.get("AUTH_DB_PATH", "/data/auth.db")
    store = await Store(db_path).open()
    svc = service.AuthService(
        store,
        _build_provider(),
        invitation_required=_flag("AUTH_INVITATION_REQUIRED"),
    )
    app.state.svc = svc
    _LOG.info("auth schema applied at %s", db_path)

    interval = int(os.environ.get("AUTH_SWEEP_INTERVAL", "3600"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(_sweeper, svc, interval)
        try:
            yield
        finally:
            _LOG.info("auth service shutting down")
            tg.cancel_scope.cancel()
    await store.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="psi-cloud auth",
        description="手机号 / 邮箱验证码免密登录。C 端桌面客户端的云端认证服务。",
        version="0.2.0",
        lifespan=lifespan,
    )

    @app.exception_handler(service.ServiceError)
    async def _on_service_error(
        _request: Request, exc: service.ServiceError
    ) -> JSONResponse:
        code, body = error_body(exc)
        return JSONResponse(body, status_code=code)

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        """契约要 400 + {"error": …}，FastAPI 默认是 422 + {"detail": …}。

        用笼统的 invalid_request：字段缺失/超长是协议层错误；语义错误
        （invalid_phone 等）由 service 给出，不在这里猜。
        """
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    app.include_router(router, prefix=PREFIX)
    # healthz 同时挂根路径：Caddy / compose 的 healthcheck 探的是 /healthz
    app.include_router(router, prefix="", include_in_schema=False)

    if _flag("AUTH_TEST_HOOKS"):
        from .test_hooks import hooks_router

        _LOG.warning("测试钩子已挂载（会回显验证码）—— 生产环境必须关闭")
        app.include_router(hooks_router, prefix="/__test__")
    return app


app = create_app()
