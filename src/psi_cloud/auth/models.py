"""请求模型。

**这里只管「字段在不在、长度有没有超」，不管语义是否合法。**

语义校验（手机号格式、邮箱格式、归一化）由 service.py 独占，原因有两条：

1. **归一化必须在限频之前**，而归一化在 service 里。若 Pydantic 先以
   「格式不对」拒掉，`+86 138…` 这种写法在到达归一化之前就被判死，
   契约要求的「三种写法命中同一限频桶」就无从成立。
2. **错误码必须是契约那 11 个之一。** Pydantic 拒绝抛的是
   RequestValidationError，翻出来只能是笼统的 invalid_request；
   只有 service 知道该报 invalid_phone 还是 invalid_email。

所以 `min_length` 一律不设 —— 空串要能穿过来，由 service 给出正确的码。
`max_length` 保留，它不是语义校验而是输入护栏（防超长串打库打日志）。

响应模型刻意不定义：契约的 /verify/* 是两种形状二选一，service 返回的 dict
就是契约形状，再套一层 Pydantic 只会引入静默裁剪字段的风险。
"""

from pydantic import BaseModel, Field


class SendSmsRequest(BaseModel):
    phone: str = Field(max_length=32)
    invitationCode: str | None = Field(default=None, max_length=64)


class SendEmailRequest(BaseModel):
    email: str = Field(max_length=254)
    invitationCode: str | None = Field(default=None, max_length=64)


class VerifyPhoneRequest(BaseModel):
    phone: str = Field(max_length=32)
    code: str = Field(max_length=8)
    deviceKey: str = Field(max_length=128)
    # platform 契约标为必填（客户端有 sys.platform），设备列表靠它区分设备
    platform: str = Field(max_length=32)


class VerifyEmailRequest(BaseModel):
    email: str = Field(max_length=254)
    code: str = Field(max_length=8)
    deviceKey: str = Field(max_length=128)
    platform: str = Field(max_length=32)


class CompleteRequest(BaseModel):
    tempToken: str = Field(max_length=256)
    deviceKey: str = Field(max_length=128)
    platform: str = Field(max_length=32)
    displayName: str | None = Field(default=None, max_length=64)
    invitationCode: str | None = Field(default=None, max_length=64)
