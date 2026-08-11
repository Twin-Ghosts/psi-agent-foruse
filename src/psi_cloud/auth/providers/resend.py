"""Resend 邮件投递。

  POST https://api.resend.com/emails
  Authorization: Bearer <AUTH_RESEND_API_KEY>
  body: { from, to, subject, html, text }

Resend 只负责投递,验证码全生命周期由本服务管(email_codes 表):
  6 位数字用 secrets 生成 / 存 HMAC 哈希不存明文 /
  10 分钟过期 / 命中即删防重放 / 试错 5 次作废

发信域名必须完成 SPF + DKIM DNS 验证,否则只能发往账号自己的注册邮箱
(未验证域名用 onboarding@resend.dev 发给第三方会被 422 validation_error 拒)。
免费额度 3000 封/月、100 封/天。

发码采用 fire-and-forget,不回报账号是否存在,避免账号枚举。

失败一律收敛成 SendResult(ok=False),不抛异常:上层 service 靠 ok 决定是否
退还限频配额,抛异常会绕过那段逻辑,把供应商故障变成用户被锁在门外。
"""

from __future__ import annotations

import json
import logging
import os
import uuid

import httpx

from .base import EmailProvider, SendResult

_LOG = logging.getLogger(__name__)

API_BASE = "https://api.resend.com"

# 验证码有效期,与 shared/config.py 的 code_ttl_seconds 默认值一致。
# 只用于邮件正文的措辞,真正的过期判定在 email_codes 表上做。
_TTL_MINUTES = 5


def _trust_env() -> bool:
    """是否让 httpx 读系统代理。默认否。

    httpx 会读系统代理设置(Windows 下连注册表里的都读,不只是 HTTP_PROXY),
    于是自检里发往 http://127.0.0.1:<port> 的请求会被塞进本机代理、假服务器
    收不到请求。生产容器里没有代理,关掉无损失;确实要走代理的环境显式设
    AUTH_HTTP_TRUST_ENV=true。
    """
    return os.environ.get("AUTH_HTTP_TRUST_ENV", "").strip().lower() in (
        "1", "true", "yes", "on")


class ResendEmailProvider(EmailProvider):
    name = "resend"

    def __init__(self, *, api_key: str, sender: str,
                 api_base: str = API_BASE) -> None:
        self._api_key = api_key
        self._sender = sender or "onboarding@resend.dev"
        self._api_base = (api_base or API_BASE).rstrip("/")
        self.sent_count = 0

    def ready(self) -> bool:
        return bool(self._api_key)

    async def send_code(self, email: str, code: str) -> SendResult:
        if not self._api_key:
            return SendResult(ok=False, reason="未配置 AUTH_RESEND_API_KEY")

        payload = {
            "from": self._sender,
            "to": [email],
            "subject": f"验证码：{code}",
            "text": (f"你的验证码是 {code}，{_TTL_MINUTES} 分钟内有效。\n\n"
                     "如果不是你本人操作，忽略本邮件即可。"),
            "html": (f'<p>你的验证码是 <strong style="font-size:20px;'
                     f'letter-spacing:3px">{code}</strong></p>'
                     f"<p>{_TTL_MINUTES} 分钟内有效。"
                     "如果不是你本人操作，忽略本邮件即可。</p>"),
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # 幂等键：重试(含 httpx 之外的上层重试)不会重复发信
            "Idempotency-Key": uuid.uuid4().hex,
        }

        self.sent_count += 1
        try:
            async with httpx.AsyncClient(timeout=20,
                                         trust_env=_trust_env()) as client:
                resp = await client.post(f"{self._api_base}/emails",
                                         content=json.dumps(payload).encode(),
                                         headers=headers)
        except Exception as exc:  # noqa: BLE001 —— 网络异常不该冒泡到路由层
            _LOG.warning("resend request failed: %r", exc)
            return SendResult(ok=False, reason="provider_unreachable")

        try:
            body = resp.json()
        except ValueError:
            body = {}

        if resp.status_code >= 400:
            # 失败体形如 {"statusCode":422,"name":..,"message":..}
            name = body.get("name") or f"http_{resp.status_code}"
            # message 可能含收件人地址，不进日志（AGENTS.md 硬规则 8）
            _LOG.warning("resend rejected: status=%s name=%s",
                         resp.status_code, name)
            return SendResult(ok=False, reason=name)

        if not body.get("id"):
            _LOG.warning("resend 200 但响应无 id")
            return SendResult(ok=False, reason="missing_id")

        return SendResult(ok=True)
