"""日志配置。不打印完整手机号 / 邮箱 / 验证码。"""

import logging
import sys

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    _CONFIGURED = True


def mask_phone(phone: str) -> str:
    if len(phone) < 7:
        return "*" * len(phone)
    return f"{phone[:3]}****{phone[-4:]}"


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "*" * len(email)
    head = local[:1] if local else ""
    return f"{head}***@{domain}"
