"""环境变量配置。凭证只从环境读,绝不落盘到任何快照文件。"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

SmsProvider = Literal["mock", "aliyun"]
EmailProvider = Literal["mock", "resend"]


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTH_", extra="ignore")

    db_path: str = "/data/auth.db"
    log_level: str = "INFO"

    # provider 选择:mock 用于开发与自动化测试,不发真短信、不花钱
    sms_provider: SmsProvider = "mock"
    email_provider: EmailProvider = "mock"

    # token
    token_ttl_days: int = 60
    temp_token_ttl_seconds: int = 600

    # 验证码
    code_length: int = 6
    code_ttl_seconds: int = 300
    code_max_attempts: int = 5

    # 限频
    send_interval_seconds: int = 60
    send_ip_limit: int = 5
    send_ip_window_seconds: int = 60
    verify_max_attempts: int = 5
    verify_window_seconds: int = 300

    # 注册门禁
    invitation_required: bool = False

    # 供应商凭证(env 注入,mock 模式下留空)
    aliyun_access_key_id: str = ""
    aliyun_access_key_secret: str = ""
    aliyun_sms_sign_name: str = ""
    aliyun_sms_template_code: str = ""
    aliyun_sms_template_param: str = '{"code":"##code##","min":"5"}'
    resend_api_key: str = ""
    resend_from: str = ""

    # 验证码哈希盐
    code_hash_salt: str = "dev-only-change-me"


class AnalyticsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ANALYTICS_", extra="ignore")

    db_path: str = "/data/analytics.db"
    log_level: str = "INFO"
    allow_origins: str = "*"


@lru_cache
def auth_settings() -> AuthSettings:
    return AuthSettings()


@lru_cache
def analytics_settings() -> AnalyticsSettings:
    return AnalyticsSettings()
