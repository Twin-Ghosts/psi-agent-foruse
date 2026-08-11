# -*- coding: utf-8 -*-
"""HTTP 层（aiohttp + anyio）。

按方案文档要求：``anyio`` 全程、禁 asyncio 原生 API、``run()`` 可取消、
``finally`` 清理资源。此前的标准库 ``ThreadingHTTPServer`` 版本已替换 ——
它每个请求占一个线程，且与仓库的 anyio 约束不一致。

路由表由 contract/auth_contract.py 驱动，不在这里另抄一份 —— 契约是唯一来源，
抄第二份就会漂移。

测试钩子（/__test__/*）只在 test_hooks=True 时挂载，生产必须关闭：它会回显验证码。
"""

import json
import os
import sys

import anyio
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import providers, service                       # noqa: E402
from app.store import Store                             # noqa: E402
from contract import auth_contract as C                  # noqa: E402


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(
        o, ensure_ascii=False))


def _client_ip(request):
    """跑在 Caddy 后面时 socket 地址是反代的，必须读转发头。

    不读的话所有用户共用一个限频桶，send_per_ip 形同虚设（或反过来，一个用户
    就能把全站发码额度打满）。
    """
    fwd = request.headers.get(C.CLIENT_IP_HEADER)
    if fwd:
        return fwd.split(",")[0].strip()
    peer = request.remote or ""
    return peer


async def _body(request):
    """读 JSON 体；非法或非对象返回 None 让调用方回 400。"""
    try:
        data = await request.json()
    except Exception:                                    # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _err(e):
    status, _msg = C.ERRORS.get(e.code, (500, ""))
    body = {"error": e.code}
    if e.retry_after is not None:
        body["retryAfter"] = e.retry_after
    return _json(body, status=status)


# ---------------------------------------------------------------- 路由处理
async def _sms_send(request):
    body = await _body(request)
    if body is None:
        return _json({"error": "invalid_json"}, 400)
    svc = request.app["svc"]
    try:
        return _json(await svc.send_code("phone", body.get("phone"),
                                         _client_ip(request),
                                         body.get("invitationCode")))
    except service.ServiceError as e:
        return _err(e)


async def _otp(request):
    body = await _body(request)
    if body is None:
        return _json({"error": "invalid_json"}, 400)
    svc = request.app["svc"]
    try:
        return _json(await svc.send_code("email", body.get("email"),
                                         _client_ip(request),
                                         body.get("invitationCode")))
    except service.ServiceError as e:
        return _err(e)


async def _verify(request, kind):
    body = await _body(request)
    if body is None:
        return _json({"error": "invalid_json"}, 400)
    svc = request.app["svc"]
    try:
        return _json(await svc.verify(kind, body.get(kind), body.get("code"),
                                      body.get("deviceKey"),
                                      body.get("platform")))
    except service.ServiceError as e:
        return _err(e)


async def _verify_phone(request):
    return await _verify(request, "phone")


async def _verify_email(request):
    return await _verify(request, "email")


async def _complete(request):
    body = await _body(request)
    if body is None:
        return _json({"error": "invalid_json"}, 400)
    svc = request.app["svc"]
    try:
        return _json(await svc.complete(body.get("tempToken"),
                                        body.get("deviceKey"),
                                        body.get("platform"),
                                        body.get("displayName"),
                                        body.get("invitationCode")))
    except service.ServiceError as e:
        return _err(e)


def _token_of(request):
    h = request.headers.get(C.AUTH_HEADER) or ""
    prefix = C.AUTH_SCHEME + " "
    return h[len(prefix):] if h.startswith(prefix) else None


async def _me(request):
    try:
        return _json(await request.app["svc"].me(_token_of(request)))
    except service.ServiceError as e:
        return _err(e)


async def _logout(request):
    try:
        return _json(await request.app["svc"].logout(_token_of(request)))
    except service.ServiceError as e:
        return _err(e)


async def _sessions_list(request):
    try:
        return _json(await request.app["svc"].list_devices(_token_of(request)))
    except service.ServiceError as e:
        return _err(e)


async def _session_revoke(request):
    try:
        return _json(await request.app["svc"].revoke_device(
            _token_of(request), request.match_info.get("id", "")))
    except service.ServiceError as e:
        return _err(e)


async def _healthz(request):
    """只探库连通性，不碰供应商 —— 否则供应商抖动会让容器被反复重启。"""
    try:
        await request.app["svc"].store.one("SELECT 1")
        return _json({"ok": True})
    except Exception as e:                               # noqa: BLE001
        return _json({"ok": False, "error": repr(e)[:120]}, 503)


# ---------------------------------------------------------------- 测试钩子
#
# 生产绝不挂载。它会回显验证码——暴露到公网等于任何人可登录任何账号。
async def _hook_code(request):
    ident = request.query.get("id", "")
    key = (service.norm_email(ident) or service.norm_phone(ident) or ident)
    return _json({"code": request.app["svc"].provider.peek_code(key)})


async def _hook_provider_calls(request):
    return _json({"count": request.app["svc"].provider.calls})


async def _hook_counts(request):
    svc = request.app["svc"]
    n = service.now_iso()

    async def q(sql, args=()):
        return (await svc.store.one(sql, args))[0]

    return _json({
        "codes": await q("SELECT COUNT(*) FROM email_codes"),
        "sessions": await q("SELECT COUNT(*) FROM sessions"),
        "users": await q("SELECT COUNT(*) FROM users"),
        "devices": await q("SELECT COUNT(*) FROM devices"),
        "expired_codes": await q(
            "SELECT COUNT(*) FROM email_codes WHERE expires_at < ?", (n,)),
        "expired_sessions": await q(
            "SELECT COUNT(*) FROM sessions WHERE expires_at < ?", (n,)),
        "expired_quota": await q(
            "SELECT COUNT(*) FROM send_quota WHERE window_start < ?",
            (service.now_iso(-3600),)),
    })


async def _hook_sweep(request):
    return _json(await request.app["svc"].sweep())


async def _hook_reset_limits(request):
    await request.app["svc"].reset_limits()
    return _json({"ok": True})


# ---------------------------------------------------------------- 装配
_HANDLERS = {
    "sms_send": _sms_send,
    "otp": _otp,
    "verify_phone": _verify_phone,
    "verify_email": _verify_email,
    "complete": _complete,
    "me": _me,
    "logout": _logout,
    "sessions_list": _sessions_list,
    "session_revoke": _session_revoke,
}


def make_app(svc, test_hooks=False):
    """按契约装配 aiohttp 应用。路由来自 auth_contract，不另抄一份。"""
    app = web.Application()
    app["svc"] = svc
    app.router.add_get("/healthz", _healthz)

    for key, spec in C.ENDPOINTS.items():
        handler = _HANDLERS[key]
        # 契约里的 {id} 就是 aiohttp 的 {id}，形状一致，直接用
        app.router.add_route(spec["method"], C.PREFIX + spec["path"], handler)

    if test_hooks:
        app.router.add_get("/__test__/code", _hook_code)
        app.router.add_get("/__test__/provider_calls", _hook_provider_calls)
        app.router.add_get("/__test__/counts", _hook_counts)
        app.router.add_post("/__test__/sweep", _hook_sweep)
        app.router.add_post("/__test__/reset_limits", _hook_reset_limits)
    return app


async def build(db=":memory:", test_hooks=False, invitation_required=False,
                provider=None):
    """建 service + app。返回 (app, svc)。"""
    if provider is None:
        if os.environ.get("AUTH_USE_REAL_PROVIDERS", "").strip().lower() in (
                "1", "true", "yes", "on"):
            from app.real_providers import RoutingProvider
            provider = RoutingProvider()
        else:
            provider = providers.MockProvider()
    store = await Store(db).open()
    svc = service.AuthService(store, provider,
                              invitation_required=invitation_required)
    return make_app(svc, test_hooks), svc


async def run(host="0.0.0.0", port=8000, db="data/auth.db", test_hooks=False,
              invitation_required=False, sweep_interval=3600):
    """起服务并阻塞。可被取消；finally 里清理 runner 与库连接。

    host 默认 0.0.0.0：容器里 Caddy 是**另一个容器**，通过服务名访问，只绑
    127.0.0.1 的话包到不了、必然 502。不对宿主暴露端口由 compose 的 expose
    （而非 ports）保证。
    """
    app, svc = await build(db=db, test_hooks=test_hooks,
                           invitation_required=invitation_required)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"认证服务已启动：http://{host}:{port}{C.PREFIX}", flush=True)
    print(f"数据库：{db}   邀请码门禁：{'开' if invitation_required else '关'}"
          f"   测试钩子：{'开' if test_hooks else '关'}", flush=True)
    print(f"发码通道：{getattr(svc.provider, 'name', '?')}", flush=True)
    if getattr(svc.provider, "name", "") == "mock":
        print("警告：当前是 mock 通道，不会真的发出邮件或短信。"
              "要接真实供应商，设 AUTH_USE_REAL_PROVIDERS=true 并配齐凭据。",
              flush=True)
    else:
        for kind in ("email", "phone"):
            p = getattr(svc.provider, kind, None)
            if p is not None and getattr(p, "name", "") == "mock":
                print(f"警告：{kind} 通道凭据不全，已回落到 mock，不会真的发送",
                      flush=True)
    if test_hooks:
        print("警告：测试钩子会暴露验证码，生产环境必须关闭", flush=True)
    if not os.environ.get("EMAIL_CODE_SALT"):
        print("警告：未设置 EMAIL_CODE_SALT，本次进程用随机盐，"
              "重启后此前发出的验证码全部失效", flush=True)

    try:
        async with anyio.create_task_group() as tg:
            if sweep_interval > 0:
                tg.start_soon(_sweep_loop, svc, sweep_interval)
            await anyio.sleep_forever()
    finally:
        with anyio.CancelScope(shield=True):
            await runner.cleanup()
            await svc.store.aclose()


async def _sweep_loop(svc, interval):
    """低频清全表。过期 email_codes / sessions / send_quota 都没有 TTL 兜底，
    只靠写入路径顺手删不够（没人再写的 key 永远留着）。异常不能让循环退出，
    否则清理静默停止且无人知晓。"""
    while True:
        await anyio.sleep(interval)
        try:
            result = await svc.sweep()
            if result.get("deleted"):
                print(f"[sweep] 清理过期数据 {result['deleted']} 行", flush=True)
        except Exception as e:                            # noqa: BLE001
            print(f"[sweep] 本轮失败（下轮继续）: {e!r}", flush=True)


if __name__ == "__main__":
    hooks = "--test-hooks" in sys.argv
    dbpath = os.environ.get("AUTH_DB", "data/auth.db")
    if dbpath != ":memory:":
        os.makedirs(os.path.dirname(dbpath) or ".", exist_ok=True)
    gate = os.environ.get("INVITATION_REQUIRED", "false").strip().lower() \
        in ("1", "true", "yes", "on")
    anyio.run(run, os.environ.get("AUTH_BIND", "0.0.0.0"), 8000, dbpath,
              hooks, gate, int(os.environ.get("AUTH_SWEEP_INTERVAL", "3600")))
