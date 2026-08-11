"""阿里云号码认证服务(PNVS)短信验证码。

脚手架留空实现,业务开发时补齐。以下是已确认的接入要点:

Endpoint: dypnsapi.aliyuncs.com  命名空间 dypnsapi-2017-05-25  RPC 风格

发码 SendSmsVerifyCode 参数:
  phoneNumber   归一化后的 11 位号
  signName      系统签名名称(env 注入)
  templateCode  系统模板 code(env 注入)
  templateParam {"code":"##code##","min":"5"}  ##code## 由阿里云填入实际码
                变量名必须与所选模板定义一致(系统模板变量名固定)
  codeLength 6 / codeType 1 / interval 60 / validTime 300

系统签名与自有签名走的是同两个字段,区别只在填什么值、要不要审批,
因此实现不分叉:一律从 env 读,换自有签名只改 env。

校验 CheckSmsVerifyCode { phoneNumber, verifyCode }:
  ** 成功条件是两层同时满足 **
    body.code == "OK"  且  body.model.verifyResult == "PASS"
  只判外层会把「码错」当成「校验成功」,这是必须避开的坑。

需识别的错误码(从异常的 data.Code 取,不在正常返回里):
  FREQUENCY_FAIL          发送过于频繁
  BUSINESS_LIMIT_CONTROL  当日上限

不引官方 SDK(同步实现且拖传递依赖),用 httpx 自写客户端,
RPC 签名算法版本以控制台文档为准。
"""

from .base import CheckResult, SendResult, SmsProvider


class AliyunSmsProvider(SmsProvider):
    def __init__(
        self,
        *,
        access_key_id: str,
        access_key_secret: str,
        sign_name: str,
        template_code: str,
        template_param: str,
    ) -> None:
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._sign_name = sign_name
        self._template_code = template_code
        self._template_param = template_param

    async def send_code(self, phone: str) -> SendResult:
        raise NotImplementedError("AliyunSmsProvider.send_code 待实现")

    async def check_code(self, phone: str, code: str) -> CheckResult:
        raise NotImplementedError("AliyunSmsProvider.check_code 待实现")
