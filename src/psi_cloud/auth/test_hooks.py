"""测试钩子。**生产绝不挂载。**

它会回显验证码 —— 暴露到公网等于任何人可登录任何账号。挂载条件是
`AUTH_TEST_HOOKS=true`，默认关闭；服务器验收清单第 2 条验的就是公网访问
`/__test__/provider_calls` 返回 404。

存在的理由：契约测试要断言「限频发生在调用供应商之前」，那需要知道供应商
被调了几次；也要能取到 mock 发出的验证码来走完登录流程。这些都不能靠猜。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from . import service

hooks_router = APIRouter(include_in_schema=False)


def _svc(request: Request) -> Any:
    return request.app.state.svc


@hooks_router.get("/code")
async def hook_code(request: Request, id: str = "") -> dict[str, Any]:
    """取某个 identifier 当前的验证码。

    按 service 的归一化规则找 key —— 否则 `+86 138…` 取不到用
    `138…` 存的那条，测试会以为是业务坏了。
    """
    key = service.norm_email(id) or service.norm_phone(id) or id
    return {"code": _svc(request).provider.peek_code(key)}


@hooks_router.get("/provider_calls")
async def hook_provider_calls(request: Request) -> dict[str, Any]:
    return {"count": _svc(request).provider.calls}


@hooks_router.get("/counts")
async def hook_counts(request: Request) -> dict[str, Any]:
    svc = _svc(request)
    now = service.now_iso()

    async def q(sql: str, args: tuple[Any, ...] = ()) -> int:
        row = await svc.store.one(sql, args)
        return int(row[0])

    return {
        "codes": await q("SELECT COUNT(*) FROM email_codes"),
        "sessions": await q("SELECT COUNT(*) FROM sessions"),
        "users": await q("SELECT COUNT(*) FROM users"),
        "devices": await q("SELECT COUNT(*) FROM devices"),
        "expired_codes": await q(
            "SELECT COUNT(*) FROM email_codes WHERE expires_at < ?", (now,)),
        "expired_sessions": await q(
            "SELECT COUNT(*) FROM sessions WHERE expires_at < ?", (now,)),
        "expired_quota": await q(
            "SELECT COUNT(*) FROM send_quota WHERE window_start < ?",
            (service.now_iso(-3600),)),
    }


@hooks_router.post("/sweep")
async def hook_sweep(request: Request) -> dict[str, Any]:
    return await _svc(request).sweep()


@hooks_router.post("/reset_limits")
async def hook_reset_limits(request: Request) -> dict[str, Any]:
    await _svc(request).reset_limits()
    return {"ok": True}
