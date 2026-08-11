"""mock provider:开发与自动化测试用,不发真短信、不花钱。

固定码 000000,并把最后一次发送记录在内存里供测试读取。
生产环境误用会在启动日志里出现显著告警(见 app.py)。
"""

import logging

from .base import CheckResult, EmailProvider, SendResult, SmsProvider

_LOG = logging.getLogger(__name__)

FIXED_CODE = "000000"


class MockSmsProvider(SmsProvider):
    def __init__(self) -> None:
        self.last_phone: str = ""
        self.sent_count: int = 0

    async def send_code(self, phone: str) -> SendResult:
        self.last_phone = phone
        self.sent_count += 1
        _LOG.info("mock sms sent, fixed code in use")
        return SendResult(ok=True)

    async def check_code(self, phone: str, code: str) -> CheckResult:
        if code == FIXED_CODE:
            return CheckResult(ok=True)
        return CheckResult(ok=False, reason="code_mismatch")


class MockEmailProvider(EmailProvider):
    def __init__(self) -> None:
        self.last_email: str = ""
        self.last_code: str = ""
        self.sent_count: int = 0

    async def send_code(self, email: str, code: str) -> SendResult:
        self.last_email = email
        self.last_code = code
        self.sent_count += 1
        _LOG.info("mock email sent")
        return SendResult(ok=True)
