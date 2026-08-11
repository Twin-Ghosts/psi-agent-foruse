"""供应商接口。

手机与邮箱的验证码生命周期归属不同,这是两个 Protocol 而非一个的原因:

- 短信走阿里云 PNVS,验证码的生成、存储、有效期、校验全在阿里云侧,
  我们零验证码存储 —— 所以有 check_code。
- 邮件走 Resend,Resend 只负责投递,验证码全生命周期由我们管 ——
  所以只有 send,校验在本服务的 email_codes 表上做。
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SendResult:
    ok: bool
    # 供应商侧限频/上限等业务失败,用于回给客户端可读原因
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    reason: str = ""


class SmsProvider(Protocol):
    """短信:发码与校验都在供应商侧。"""

    async def send_code(self, phone: str) -> SendResult: ...

    async def check_code(self, phone: str, code: str) -> CheckResult: ...


class EmailProvider(Protocol):
    """邮件:仅投递。code 由本服务生成并校验。"""

    async def send_code(self, email: str, code: str) -> SendResult: ...
