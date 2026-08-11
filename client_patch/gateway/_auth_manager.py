"""AuthManager —— 登录态持有者 + 云端认证服务客户端。

**它不是微内核组件。** ``psi_agent/`` 下的顶层包 (``ai`` / ``channel`` /
``gateway`` / ``router`` / ``session``) 各有自己的 ``run()``、自己的 socket、
独立进程; 认证没有这些 —— 没 socket、没 ``run()``、不独立部署, 生命周期完全跟随
``Gateway.run()``。所以它是个 **Gateway manager**, 与 ``TitleManager`` /
``OAuthRelay`` 同级, 沿用 ``_xxx_manager.py`` 命名与平铺结构。

职责边界 (刻意窄):

- 只跟云端认证服务通 HTTP, **不持任何供应商密钥** —— 安装包里放阿里云 AK/SK 或
  Resend key 等于公开发布, 发码必须由云端代理。
- 只管「谁登录了」, 不碰 Session 层。用户数据 (会话历史/todos/workspace) 全部留在
  本机, 本期不做云端同步, ``Session`` 不加 owner 字段。
- 手机号与邮箱验证码**全程在应用内完成, 不开浏览器跳转**: OTP 不是第三方授权,
  号码和验证码本来就输在自己的界面里。跳转留给将来的 OAuth (那时复用 ``OAuthRelay``)。

``endpoint`` 为空时 ``Gateway`` 根本不创建本 manager、也不注册 ``/auth/*``, 现有
本地单用户流程零回归。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import anyio
from loguru import logger

from psi_agent.gateway._auth_store import AuthStore

# 客户端拿到 401 即视为登录态失效: 清本地凭证、回登录界面。没有静默续期逻辑 ——
# 云端是滑动过期 (每次请求刷 last_used_at), 60 天内正常使用不会掉线。
_UNAUTHORIZED = 401

# 云端认证服务的统一前缀, 与 psi-agent-auth 的契约一致。
_PREFIX = "/api/auth"

# 单次请求上限。发码要过云端再到供应商, 给宽松些; 但必须有界, 否则网络黑洞会挂住
# 整个 Gateway 请求。
_TIMEOUT_SECONDS = 30.0


@dataclass
class AuthManager:
    """持有登录态, 代理云端认证 API。"""

    endpoint: str = ""
    _store: AuthStore | None = None
    _token: str = ""
    _device_key: str = ""
    _platform: str = ""
    _lock: anyio.Lock = field(default_factory=anyio.Lock)
    _session: aiohttp.ClientSession | None = None

    @classmethod
    async def create(cls, endpoint: str, appdata_root: str = "", platform: str = "") -> AuthManager:
        """建一个 manager 并从磁盘恢复登录态 (满足 R3: 跨重启保持)。"""
        store = await AuthStore.from_appdata(appdata_root)
        token = await store.load_token()
        device_key = await store.device_key()
        mgr = cls(
            endpoint=endpoint.rstrip("/"),
            _store=store,
            _token=token,
            _device_key=device_key,
            _platform=platform or sys.platform,
        )
        if token:
            logger.info("已从本机凭证恢复登录态 (未回验, 首次请求 401 时再清)")
        return mgr

    async def aclose(self) -> None:
        """释放 HTTP 会话。``Gateway.run`` 的 finally 里调用。"""
        if self._session is not None and not self._session.closed:
            with anyio.CancelScope(shield=True):
                await self._session.close()
        self._session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
            )
        return self._session

    async def _call(
        self, method: str, path: str, payload: dict[str, Any] | None = None, *, auth: bool = False
    ) -> tuple[int, dict[str, Any]]:
        """发一次云端请求, 返回 ``(状态码, 响应体)``。

        网络异常收敛成 ``(0, {"error": ...})`` —— 调用方 (HTTP 路由) 据此回 502,
        而不是让异常冒到 aiohttp 中间件变成 500。
        """
        if not self.endpoint:
            return 0, {"error": "auth_endpoint_not_configured"}
        headers: dict[str, str] = {}
        if auth:
            if not self._token:
                return _UNAUTHORIZED, {"error": "unauthorized"}
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"{self.endpoint}{_PREFIX}{path}"
        try:
            session = self._ensure_session()
            async with session.request(method, url, json=payload, headers=headers) as resp:
                try:
                    body = await resp.json()
                except Exception:
                    text = await resp.text()
                    body = {"error": "bad_response", "detail": text[:200]}
                if not isinstance(body, dict):
                    body = {"error": "bad_response"}
                return resp.status, body
        except Exception as e:
            logger.warning(f"认证服务请求失败 {method} {path}: {e!r}")
            return 0, {"error": "upstream_unreachable", "detail": repr(e)[:200]}

    async def _on_response(self, status: int) -> None:
        """401 即清本地凭证 —— 云端撤销设备后, 本机不该继续显示已登录。"""
        if status == _UNAUTHORIZED and self._token:
            logger.info("云端返回 401, 清除本机登录态")
            await self.logout_local()

    # ---- 发码 / 校验 ----
    async def send_code(self, *, phone: str = "", email: str = "", invitation: str = "") -> tuple[int, dict[str, Any]]:
        """请云端发验证码。手机号与邮箱二选一。"""
        if phone:
            payload: dict[str, Any] = {"phone": phone}
            path = "/sms/send"
        elif email:
            payload = {"email": email}
            path = "/otp"
        else:
            return 400, {"error": "phone_or_email_required"}
        if invitation:
            payload["invitationCode"] = invitation
        return await self._call("POST", path, payload)

    async def verify(
        self, *, code: str, phone: str = "", email: str = ""
    ) -> tuple[int, dict[str, Any]]:
        """校验验证码。老用户直接拿到 token; 新用户拿到 ``tempToken``。"""
        if not code:
            return 400, {"error": "code_required"}
        if phone:
            payload: dict[str, Any] = {"phone": phone}
            path = "/verify/phone"
        elif email:
            payload = {"email": email}
            path = "/verify/email"
        else:
            return 400, {"error": "phone_or_email_required"}
        payload.update({"code": code, "deviceKey": self._device_key, "platform": self._platform})
        status, body = await self._call("POST", path, payload)
        if status == 200 and body.get("token"):
            await self._adopt_token(str(body["token"]))
        return status, body

    async def complete(
        self, *, temp_token: str, display_name: str = "", invitation: str = ""
    ) -> tuple[int, dict[str, Any]]:
        """两段式注册的第二段: 建号并换正式 token。"""
        if not temp_token:
            return 400, {"error": "temp_token_required"}
        payload: dict[str, Any] = {
            "tempToken": temp_token,
            "deviceKey": self._device_key,
            "platform": self._platform,
        }
        if display_name:
            payload["displayName"] = display_name
        if invitation:
            payload["invitationCode"] = invitation
        status, body = await self._call("POST", "/complete", payload)
        if status == 200 and body.get("token"):
            await self._adopt_token(str(body["token"]))
        return status, body

    async def _adopt_token(self, token: str) -> None:
        async with self._lock:
            self._token = token
        if self._store is not None:
            await self._store.save_token(token)
        logger.info("登录成功, 凭证已落盘")

    # ---- 已登录接口 ----
    async def me(self) -> tuple[int, dict[str, Any]]:
        status, body = await self._call("GET", "/me", auth=True)
        await self._on_response(status)
        return status, body

    async def list_devices(self) -> tuple[int, dict[str, Any]]:
        status, body = await self._call("GET", "/sessions", auth=True)
        await self._on_response(status)
        return status, body

    async def revoke_device(self, device_id: str) -> tuple[int, dict[str, Any]]:
        if not device_id:
            return 400, {"error": "device_id_required"}
        status, body = await self._call("DELETE", f"/sessions/{device_id}", auth=True)
        await self._on_response(status)
        return status, body

    async def logout(self) -> tuple[int, dict[str, Any]]:
        """通知云端撤销本会话, 然后清本机凭证。

        云端不可达时也要清本机 —— 否则用户点了登出却仍显示已登录, 比多留一条
        云端会话更糟 (那条会话 60 天后自然过期)。
        """
        status, body = await self._call("POST", "/logout", auth=True)
        await self.logout_local()
        if status == 0:
            logger.warning("云端不可达, 已仅清除本机登录态")
        return (200 if status == 0 else status), (body if status else {"ok": True})

    async def logout_local(self) -> None:
        async with self._lock:
            self._token = ""
        if self._store is not None:
            await self._store.clear_token()

    # ---- 状态 ----
    def status(self) -> dict[str, Any]:
        """给 SPA 判断该显示登录引导还是身份信息。不含 token 本身。"""
        return {
            "endpoint": self.endpoint,
            "loggedIn": bool(self._token),
            "deviceKey": self._device_key,
            "platform": self._platform,
            # 钥匙串不可用时如实上报, 让界面能提示「凭证未加密」而非假装安全
            "credentialEncrypted": bool(self._store.encrypted) if self._store else False,
        }
