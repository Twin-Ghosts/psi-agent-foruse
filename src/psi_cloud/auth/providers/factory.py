"""按配置装配 provider。SMS_PROVIDER / EMAIL_PROVIDER 两个开关。"""

from ...shared.config import AuthSettings
from .base import EmailProvider, SmsProvider
from .mock import MockEmailProvider, MockSmsProvider


def build_sms_provider(settings: AuthSettings) -> SmsProvider:
    if settings.sms_provider == "mock":
        return MockSmsProvider()
    from .aliyun import AliyunSmsProvider

    return AliyunSmsProvider(
        access_key_id=settings.aliyun_access_key_id,
        access_key_secret=settings.aliyun_access_key_secret,
        sign_name=settings.aliyun_sms_sign_name,
        template_code=settings.aliyun_sms_template_code,
        template_param=settings.aliyun_sms_template_param,
    )


def build_email_provider(settings: AuthSettings) -> EmailProvider:
    if settings.email_provider == "mock":
        return MockEmailProvider()
    from .resend import ResendEmailProvider

    return ResendEmailProvider(
        api_key=settings.resend_api_key, sender=settings.resend_from
    )
