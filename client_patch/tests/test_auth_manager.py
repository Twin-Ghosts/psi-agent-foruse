"""AuthManager: 与云端认证服务的交互、登录态生命周期、失败兜底。

用 aiohttp 的测试工具起一个**真的** HTTP 服务当云端, 不打桩 ClientSession ——
打桩会把"请求实际发成什么样"这一层跳过, 而 deviceKey 有没有真的发出去、401 有没有
真的清凭证, 恰恰是这里最该验的东西。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from psi_agent.gateway._auth_manager import AuthManager


class FakeCloud:
    """假云端。记录收到的请求, 便于断言转发内容。"""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.revoked = False

    async def _record(self, request: web.Request) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if request.can_read_body:
            try:
                body = await request.json()
            except Exception:
                body = {}
        self.seen.append(
            {
                "path": request.path,
                "body": body,
                "auth": request.headers.get("Authorization"),
            }
        )
        return body

    async def send_code(self, request: web.Request) -> web.Response:
        await self._record(request)
        return web.json_response({"retryAfter": 60})

    async def verify(self, request: web.Request) -> web.Response:
        body = await self._record(request)
        if body.get("code") == "000000":
            return web.json_response({"error": "invalid_code"}, status=401)
        if body.get("code") == "old-user":
            return web.json_response({"token": "cloud-token-1", "user": {"id": "u1"}})
        return web.json_response({"tempToken": "tt-1", "isNewUser": True})

    async def complete(self, request: web.Request) -> web.Response:
        body = await self._record(request)
        if body.get("tempToken") != "tt-1":
            return web.json_response({"error": "temp_token_invalid"}, status=401)
        return web.json_response({"token": "cloud-token-2", "user": {"id": "u2"}})

    async def authed(self, request: web.Request) -> web.Response:
        await self._record(request)
        if self.revoked or not (request.headers.get("Authorization") or "").startswith("Bearer "):
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.path.endswith("/me"):
            return web.json_response({"user": {"id": "u2"}, "identities": []})
        if request.path.endswith("/sessions"):
            return web.json_response({"devices": [{"id": "d1", "current": True}]})
        return web.json_response({"ok": True})


@pytest.fixture
async def cloud():
    """起假云端, 返回 (FakeCloud, base_url)。

    自己用 AppRunner 起, 不用 ``aiohttp_server`` fixture —— 那个来自
    ``pytest-aiohttp``, 而仓库的 dev 依赖里没有它。少一个依赖, 测试就少一处
    "在别人机器上跑不起来"的可能。
    """
    fake = FakeCloud()
    app = web.Application()
    p = "/api/auth"
    app.router.add_post(f"{p}/sms/send", fake.send_code)
    app.router.add_post(f"{p}/otp", fake.send_code)
    app.router.add_post(f"{p}/verify/phone", fake.verify)
    app.router.add_post(f"{p}/verify/email", fake.verify)
    app.router.add_post(f"{p}/complete", fake.complete)
    app.router.add_get(f"{p}/me", fake.authed)
    app.router.add_post(f"{p}/logout", fake.authed)
    app.router.add_get(f"{p}/devices", fake.authed)
    app.router.add_get(f"{p}/sessions", fake.authed)
    app.router.add_delete(f"{p}/sessions/{{id}}", fake.authed)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield fake, f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.fixture
async def mgr_factory(tmp_path: Path):
    """产出 manager, 并在测试结束时关掉 aiohttp 会话。"""
    created: list[AuthManager] = []

    async def make(endpoint: str) -> AuthManager:
        m = await AuthManager.create(endpoint, appdata_root=str(tmp_path))
        created.append(m)
        return m

    yield make
    for m in created:
        await m.aclose()


async def test_starts_logged_out(cloud, mgr_factory) -> None:
    _fake, base = cloud
    mgr = await mgr_factory(base)

    status = mgr.status()
    assert status["loggedIn"] is False
    assert status["deviceKey"]
    # status 不该带 token: SPA 拿到即等于把凭证暴露给页面脚本
    assert "token" not in status


async def test_verify_forwards_device_identity(cloud, mgr_factory) -> None:
    """deviceKey / platform 必须真的发出去 —— 云端靠它把会话绑到设备,
    R5 的设备列表与撤销都依赖这一步。"""
    fake, base = cloud
    mgr = await mgr_factory(base)

    await mgr.verify(email="u@example.com", code="123456")

    sent = fake.seen[-1]["body"]
    assert sent["deviceKey"]
    assert sent["platform"]


async def test_new_user_two_stage(cloud, mgr_factory) -> None:
    """新用户先拿 tempToken, 此时还不算登录; /complete 之后才是。"""
    _fake, base = cloud
    mgr = await mgr_factory(base)

    status, body = await mgr.verify(email="u@example.com", code="123456")
    assert status == 200
    assert body["tempToken"] == "tt-1"
    assert mgr.status()["loggedIn"] is False

    status, body = await mgr.complete(temp_token="tt-1")
    assert status == 200
    assert mgr.status()["loggedIn"] is True


async def test_wrong_code_does_not_log_in(cloud, mgr_factory) -> None:
    _fake, base = cloud
    mgr = await mgr_factory(base)

    status, _body = await mgr.verify(email="u@example.com", code="000000")

    assert status == 401
    assert mgr.status()["loggedIn"] is False


async def test_login_survives_restart(cloud, mgr_factory, tmp_path: Path) -> None:
    """R3: 登录态跨重启保持。新建 manager 等价于客户端重启。"""
    _fake, base = cloud
    mgr = await mgr_factory(base)
    await mgr.verify(email="u@example.com", code="old-user")
    assert mgr.status()["loggedIn"] is True
    device_key = mgr.status()["deviceKey"]

    restarted = await mgr_factory(base)

    assert restarted.status()["loggedIn"] is True
    assert restarted.status()["deviceKey"] == device_key


async def test_cloud_401_clears_local_credentials(cloud, mgr_factory) -> None:
    """云端撤销该设备后, 本机不该继续显示已登录。"""
    fake, base = cloud
    mgr = await mgr_factory(base)
    await mgr.verify(email="u@example.com", code="old-user")

    fake.revoked = True
    status, _body = await mgr.me()

    assert status == 401
    assert mgr.status()["loggedIn"] is False


async def test_logout_clears_even_if_cloud_unreachable(cloud, mgr_factory) -> None:
    """云端不可达时登出仍要清本机 —— 否则用户点了登出却仍显示已登录,
    比多留一条云端会话更糟 (那条 60 天后自然过期)。"""
    _fake, base = cloud
    mgr = await mgr_factory(base)
    await mgr.verify(email="u@example.com", code="old-user")
    assert mgr.status()["loggedIn"] is True

    mgr.endpoint = "http://127.0.0.1:1"  # 指向没人监听的端口
    status, _body = await mgr.logout()

    assert mgr.status()["loggedIn"] is False
    assert status == 200  # 对调用方仍报成功


async def test_unreachable_cloud_is_reported_not_raised(mgr_factory) -> None:
    """网络异常收敛成 (0, upstream_unreachable), 不让异常冒到 HTTP 层变 500。"""
    mgr = await mgr_factory("http://127.0.0.1:1")

    status, body = await mgr.send_code(email="u@example.com")

    assert status == 0
    assert body["error"] == "upstream_unreachable"


async def test_missing_endpoint_makes_no_request(mgr_factory) -> None:
    """未配 --auth-endpoint 时不该发请求。"""
    mgr = await mgr_factory("")

    status, body = await mgr.send_code(email="u@example.com")

    assert status == 0
    assert "not_configured" in body["error"]


async def test_prefix_is_configurable(monkeypatch, tmp_path: Path) -> None:
    """线上存在两种形态: 本仓库契约用 /api/auth/*, psi-cloud 直接在根路径提供
    /otp。前缀写死会导致对着其中一种部署全部 404, 而客户端一旦发布就改不动。
    """
    # 必须在 setenv 之后才导入/重载, 故延迟 (noqa PLC0415)
    import importlib  # noqa: PLC0415

    monkeypatch.setenv("PSI_AUTH_PREFIX", "")
    import psi_agent.gateway._auth_manager as mod  # noqa: PLC0415

    importlib.reload(mod)
    try:
        mgr = await mod.AuthManager.create("http://x.invalid", appdata_root=str(tmp_path))
        try:
            assert mgr.status()["prefix"] == ""
        finally:
            await mgr.aclose()
    finally:
        monkeypatch.delenv("PSI_AUTH_PREFIX", raising=False)
        importlib.reload(mod)
