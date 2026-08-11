"""标识归一化。

** 归一化必须在入库和限频之前 **,否则同一个人能注册出多个账号
(+8613800138000 与 13800138000 会各建一个),也能靠变形绕过限频。
"""

import re

_PHONE_STRIP = re.compile(r"[\s\-+]")
_PHONE_CN = re.compile(r"^1[3-9]\d{9}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


class InvalidIdentifier(ValueError):
    """标识格式不合法。"""


def normalize_phone(raw: str) -> str:
    """中国大陆手机号归一化为 11 位。海外号需换 E.164 处理,本期不支持。"""
    cleaned = _PHONE_STRIP.sub("", raw or "")
    if cleaned.startswith("0086"):
        cleaned = cleaned[4:]
    elif cleaned.startswith("86") and len(cleaned) > 11:
        cleaned = cleaned[2:]
    if not _PHONE_CN.match(cleaned):
        raise InvalidIdentifier("手机号格式不合法")
    return cleaned


def normalize_email(raw: str) -> str:
    """小写化并去 Gmail 点号(Gmail 视 a.b@gmail.com 与 ab@gmail.com 为同一邮箱)。"""
    cleaned = (raw or "").strip().lower()
    if not _EMAIL.match(cleaned):
        raise InvalidIdentifier("邮箱格式不合法")
    local, _, domain = cleaned.partition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        cleaned = f"{local}@gmail.com"
    return cleaned
