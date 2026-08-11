# -*- coding: utf-8 -*-
"""真实供应商适配：Resend（邮箱）与阿里云 PNVS（手机号）。

与 MockProvider 同接口，service.py 不用改：

    send_code(provider, identifier, code) -> (外部 id, 错误信息)
    check_hosted(identifier, code)        -> (是否通过, 错误码)   仅手机号

两者的职责边界不同，这决定了 code 参数的含义：

    邮箱  Resend 只负责投递，验证码由我们生成并管理，code 传具体值
    手机  PNVS 托管验证码，生成与校验都在阿里云侧，code 传 None

不引官方 SDK：阿里云 SDK 是同步实现且会拖一堆传递依赖，RPC 签名手写约 40 行。
这里用 httpx，换别的客户端时只改 _http 一处。

凭据全部走环境变量，不接受构造参数以外的来源，也不写进任何配置快照。
"""

import base64
import hashlib
import hmac
import json
import os
import urllib.parse
import uuid
from datetime import datetime, timezone

import httpx

from . import providers_core as providers

# ---------------------------------------------------------------- 通用


def _env(*names, default=""):
    """按顺序取第一个非空环境变量。

    为什么要多名字兜底：本模块最初在 psi-agent-auth 里跑，用的是裸名
    `RESEND_API_KEY`；移植进 psi-cloud 后，配置面（.env.example /
    docker-compose.yml / shared/config.py）统一用 `AUTH_` 前缀。只认裸名会让
    容器里注入的 `AUTH_RESEND_API_KEY` 完全读不到 —— 而且是**静默**失效：
    ready() 为 False，日志只说"凭据不全"，看上去像没配，实际是配了没读。
    """
    for name in names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return default


async def _http(method, url, headers=None, body=None, timeout=20):
    """返回 (状态码, 响应字典或原文)。异常收敛成 (0, {...})。

    用异步 HTTP 而非同步：同步调用会把事件循环卡住——发码要等供应商往返几百
    毫秒，期间整个服务停摆。用 httpx 而非 aiohttp：httpx 是本仓 requirements
    里已锁的依赖（aiohttp 不在，import 会直接 ModuleNotFoundError 起不来）。

    **trust_env 默认关闭**，这一条是踩过的坑：httpx 会读系统代理（Windows 下
    连注册表里的代理都读，不只是 HTTP_PROXY 环境变量），于是发往
    `http://127.0.0.1:<port>` 的请求被塞进本机代理，自检里的假供应商服务器
    收不到任何请求、只拿到代理返回的 502。aiohttp 默认不读代理，所以移植前
    没这个问题。生产容器里本来就没有代理，关掉它没有损失；真需要走代理的环境
    显式设 AUTH_HTTP_TRUST_ENV=true 打开。
    """
    data = body.encode("utf-8") if isinstance(body, str) else body
    trust_env = _env("AUTH_HTTP_TRUST_ENV").lower() in ("1", "true", "yes", "on")
    try:
        async with httpx.AsyncClient(timeout=timeout,
                                     trust_env=trust_env) as client:
            resp = await client.request(method, url, content=data,
                                        headers=headers or {})
            raw = resp.content
            try:
                return resp.status_code, json.loads(raw or b"{}")
            except ValueError:
                return resp.status_code, {"__raw__": raw[:300].decode(
                    "utf-8", "replace")}
    except Exception as e:                       # noqa: BLE001
        return 0, {"__error__": repr(e)[:200]}


class ResendProvider:
    """Resend 邮件通道（第 5 步）。

    验证码全生命周期由我们管，Resend 只投递。发信域名必须先在控制台完成
    DNS 验证（SPF + DKIM），否则只能发往自己的注册邮箱。
    """

    name = "resend"

    def __init__(self, api_key=None, sender=None, api_base=None):
        self.api_key = api_key or _env("AUTH_RESEND_API_KEY",
                                       "RESEND_API_KEY")
        self.sender = sender or _env("AUTH_RESEND_FROM", "RESEND_FROM",
                                     default="onboarding@resend.dev")
        self.api_base = (api_base
                         or _env("AUTH_RESEND_API_BASE", "RESEND_API_BASE",
                                 default="https://api.resend.com")
                         ).rstrip("/")
        self.calls = 0

    def ready(self):
        return bool(self.api_key)

    async def send_code(self, provider, identifier, code):
        if provider != "email":
            return None, f"ResendProvider 不处理 {provider} 通道"
        if not self.api_key:
            return None, "未配置 AUTH_RESEND_API_KEY"
        if code is None:
            return None, "邮箱通道必须传入验证码（我们自管，不由供应商生成）"

        self.calls += 1
        minutes = max(1, providers.CODE_TTL // 60)
        payload = {
            "from": self.sender,
            "to": [identifier],
            "subject": f"验证码：{code}",
            "text": (f"你的验证码是 {code}，{minutes} 分钟内有效。\n\n"
                     "如果不是你本人操作，忽略本邮件即可。"),
            "html": (f'<p>你的验证码是 <strong style="font-size:20px;'
                     f'letter-spacing:3px">{code}</strong></p>'
                     f"<p>{minutes} 分钟内有效。"
                     "如果不是你本人操作，忽略本邮件即可。</p>"),
        }
        status, body = await _http(
            "POST", f"{self.api_base}/emails",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     # 幂等键：重试时不会重复发信
                     "Idempotency-Key": uuid.uuid4().hex},
            body=json.dumps(payload))

        if status == 0:
            return None, f"请求异常: {body.get('__error__')}"
        if status >= 400:
            # Resend 失败体形如 {"statusCode":422,"name":..,"message":..}
            return None, (f"HTTP {status} {body.get('name', '')}: "
                          f"{body.get('message') or str(body)[:150]}")
        mail_id = body.get("id")
        return mail_id, (None if mail_id else "响应中无 id")

    async def check_hosted(self, identifier, code):
        # 邮箱验证码由 service 层的本地状态机校验，不该走到这里
        raise NotImplementedError("邮箱验证码由本地状态机校验")

    def peek_code(self, identifier):
        return None          # 真实通道不暴露验证码


def percent_encode(s):
    """RPC 签名要求的百分号编码：RFC3986 基础上 + * ~ 三个字符特殊处理。

    quote 默认不编码 '/'，这里必须编码，故 safe=''。
    """
    encoded = urllib.parse.quote(str(s), safe="", encoding="utf-8")
    return encoded.replace("+", "%20").replace("*", "%2A").replace("%7E", "~")


def build_string_to_sign(params, method="POST"):
    """规范化查询串按参数名排序，键值分别编码后 & 连接；整串再编码，
    与 HTTP 方法和 %2F 用 & 拼接。Signature 自身不参与签名。"""
    items = sorted((k, v) for k, v in params.items()
                   if k != "Signature" and v is not None)
    canonical = "&".join(f"{percent_encode(k)}={percent_encode(v)}"
                         for k, v in items)
    return f"{method}&{percent_encode('/')}&{percent_encode(canonical)}"


def sign(params, secret, method="POST"):
    """HMAC-SHA1(待签串, secret + '&') 再 Base64。

    密钥尾部那个 & 是 RPC v1.0 的规定，漏掉会得到 SignatureDoesNotMatch。
    """
    digest = hmac.new((secret + "&").encode("utf-8"),
                      build_string_to_sign(params, method).encode("utf-8"),
                      hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


class PnvsProvider:
    """阿里云号码认证服务短信验证码通道（第 6 步）。

    **验证码由阿里云托管**：生成、存储、有效期、校验全在其侧，我们零存储。
    走系统签名 + 系统模板，免资质审批；signName / templateCode 一律从环境变量
    读，将来换自有签名只改环境变量、不动代码。

    签名已对过阿里云官方文档《V2版本RPC风格API请求的请求体与签名机制》的固定
    参数示例（testid / testsecret → 9NaGiOspFP5UPcwX8Iwt2YJXXuk=），逐字节一致。
    该向量已固化进 自检_供应商.py 的 [0] 段作为常驻回归项。
    """

    name = "pnvs"

    # 需要识别的业务错误码，取自异常返回体的 Code 字段
    ERR_FREQUENCY = "FREQUENCY_FAIL"              # 发送过于频繁
    ERR_DAILY_LIMIT = "BUSINESS_LIMIT_CONTROL"    # 当日上限

    def __init__(self, access_key_id=None, access_key_secret=None,
                 sign_name=None, template_code=None, api_url=None):
        self.access_key_id = (access_key_id
                              or _env("AUTH_ALIYUN_ACCESS_KEY_ID",
                                      "ALIYUN_ACCESS_KEY_ID"))
        self.access_key_secret = (
            access_key_secret
            or _env("AUTH_ALIYUN_ACCESS_KEY_SECRET",
                    "ALIYUN_ACCESS_KEY_SECRET"))
        self.sign_name = sign_name or _env("AUTH_ALIYUN_SMS_SIGN_NAME",
                                           "DYPNS_SIGN_NAME")
        self.template_code = (template_code
                              or _env("AUTH_ALIYUN_SMS_TEMPLATE_CODE",
                                      "DYPNS_TEMPLATE_CODE"))
        self.api_url = (api_url
                        or _env("DYPNS_API_URL",
                                default="https://dypnsapi.aliyuncs.com/"))
        self.api_version = _env("DYPNS_API_VERSION", default="2017-05-25")
        self.calls = 0

    def ready(self):
        return bool(self.access_key_id and self.access_key_secret
                    and self.sign_name and self.template_code)

    async def _call(self, action, **kwargs):
        """发一次 RPC 调用，返回 (响应体, 错误信息)。"""
        if not self.access_key_id or not self.access_key_secret:
            return None, "未配置 ALIYUN_ACCESS_KEY_ID / SECRET"
        params = {
            "Action": action,
            "Version": self.api_version,
            "Format": "JSON",
            "RegionId": os.environ.get("DYPNS_REGION_ID", "cn-hangzhou"),
            "AccessKeyId": self.access_key_id,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": uuid.uuid4().hex,
            "Timestamp": datetime.now(timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        params.update({k: v for k, v in kwargs.items() if v is not None})
        params["Signature"] = sign(params, self.access_key_secret, "POST")

        status, body = await _http(
            "POST", self.api_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urllib.parse.urlencode(params))

        if status == 0:
            return None, f"请求异常: {body.get('__error__')}"
        code = body.get("Code")
        if status >= 400 or (code and code != "OK"):
            return body, f"{code or f'HTTP {status}'}: {body.get('Message', '')}"
        return body, None


    async def send_code(self, provider, identifier, code):
        """发送短信验证码。code 必须为 None —— 验证码由阿里云生成。"""
        if provider != "phone":
            return None, f"PnvsProvider 不处理 {provider} 通道"
        if code is not None:
            return None, "手机号通道的验证码由 PNVS 托管，不应由调用方生成"
        if not self.sign_name or not self.template_code:
            return None, "未配置 DYPNS_SIGN_NAME / DYPNS_TEMPLATE_CODE"

        self.calls += 1
        minutes = max(1, providers.CODE_TTL // 60)
        body, err = await self._call(
            "SendSmsVerifyCode",
            PhoneNumber=identifier,
            SignName=self.sign_name,
            TemplateCode=self.template_code,
            # ##code## 是占位符，由阿里云填入实际验证码；
            # 变量名必须与所选模板的定义一致（系统模板的变量名固定）
            TemplateParam=json.dumps({"code": "##code##",
                                      "min": str(minutes)}),
            CodeLength=str(providers.CODE_LENGTH),
            CodeType="1",
            Interval="60",
            ValidTime=str(providers.CODE_TTL))
        if err:
            return None, self._friendly(err)
        model = (body or {}).get("Model") or {}
        biz_id = model.get("BizId") or (body or {}).get("RequestId")
        return biz_id, None

    async def check_hosted(self, identifier, code):
        """校验托管验证码，返回 (是否通过, 错误码)。

        **成功条件是两层同时满足**：外层 Code == 'OK' 且
        Model.VerifyResult == 'PASS'。只判外层会把"码错"当成"校验成功"——
        这是本适配层最容易出错、后果最严重的一处。
        """
        body, err = await self._call("CheckSmsVerifyCode",
                              PhoneNumber=identifier, VerifyCode=str(code))
        if err:
            # 外层就失败：区分限频与其它，交给上层映射契约错误码
            if self.ERR_FREQUENCY in err or self.ERR_DAILY_LIMIT in err:
                return False, "rate_limited"
            return False, "invalid_code"
        model = (body or {}).get("Model") or {}
        result = model.get("VerifyResult")
        if result is None:
            return False, "invalid_code"        # 拿不到判据就当不通过
        if result == "PASS":
            return True, None
        # PNVS 对"码过期"与"码错误"都用 VerifyResult 表达
        return False, ("code_expired" if result == "UNKNOWN"
                       else "invalid_code")

    def peek_code(self, identifier):
        return None          # 真实通道不暴露验证码

    def _friendly(self, err):
        if self.ERR_FREQUENCY in err:
            return "发送过于频繁（供应商侧限频）"
        if self.ERR_DAILY_LIMIT in err:
            return "已达当日发送上限（供应商侧限制）"
        return err


def from_env():
    """按环境变量挑通道：凭据齐备就用真实的，否则回落到 mock。

    回落是有意的：开发与 CI 不该因为没有凭据而跑不起来。但生产必须确认
    ready() 为真 —— 见 自检_部署.py 对生产默认值的断言。
    """
    resend = ResendProvider()
    pnvs = PnvsProvider()
    return {
        "email": resend if resend.ready() else providers.MockProvider(),
        "phone": pnvs if pnvs.ready() else providers.MockProvider(),
    }


class RoutingProvider:
    """按通道分发到不同供应商。service.py 只认一个 provider 对象。"""

    name = "routing"

    def __init__(self, email=None, phone=None):
        chosen = from_env()
        self.email = email or chosen["email"]
        self.phone = phone or chosen["phone"]

    @property
    def calls(self):
        return getattr(self.email, "calls", 0) + getattr(self.phone, "calls", 0)

    def _pick(self, provider):
        return self.email if provider == "email" else self.phone

    async def send_code(self, provider, identifier, code):
        return await self._pick(provider).send_code(provider, identifier, code)

    async def check_hosted(self, identifier, code):
        return await self.phone.check_hosted(identifier, code)

    def peek_code(self, identifier):
        for p in (self.email, self.phone):
            got = getattr(p, "peek_code", lambda _i: None)(identifier)
            if got:
                return got
        return None
