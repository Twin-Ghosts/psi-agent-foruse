# -*- coding: utf-8 -*-
"""真实供应商适配层自检（第 5、6 步可本地验证的部分）。

**能在这里验**：RPC 签名规则、请求体字段、响应解析、错误码映射、两层成功判据、
通道分发、凭据缺失时的行为。做法是起本地 mock 端点顶替 api.resend.com 与
dypnsapi.aliyuncs.com，且 mock 端**用一套独立写法重算签名**再比对 —— 否则就是
自己跟自己对答案。

**只能用真实凭据验**：真的收到邮件 / 短信。那是服务器上的事，见
deploy/验收清单.md；本文件不假装覆盖。

    python 自检_供应商.py
    python 自检_供应商.py --negative
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import threading

import anyio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from app import providers, real_providers as R      # noqa: E402

PASS, FAIL = [], []
RESULTS = []
_SECTION = ""

AK = "test-ak-id"
SK = "test-ak-secret"
RESEND_KEY = "re_test_key"
SEEN = []           # mock 收到的请求


def section(name):
    global _SECTION
    _SECTION = name
    print(f"\n{name}")


def check(name, cond, detail=""):
    if callable(cond):
        try:
            cond = cond()
        except Exception as e:
            cond, detail = False, f"{type(e).__name__}: {e}"
    ok = bool(cond)
    RESULTS.append({"section": _SECTION, "name": name, "ok": ok,
                    "detail": "" if ok else str(detail)})
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}"
          + (f"  {detail}" if detail and not ok else ""))


def independent_sign(params, secret, method="POST"):
    """独立重写一遍 RPC 签名，避免与被测代码共享同一个 bug。"""
    def enc(v):
        return quote(str(v), safe="-_.~", encoding="utf-8")
    q = "&".join(f"{enc(k)}={enc(params[k])}"
                 for k in sorted(params) if k != "Signature")
    sts = method + "&" + enc("/") + "&" + enc(q)
    return base64.b64encode(hmac.new((secret + "&").encode(), sts.encode(),
                                     hashlib.sha1).digest()).decode()


class Handler(BaseHTTPRequestHandler):
    """同时充当 Resend 与 PNVS 的 mock。"""

    protocol_version = "HTTP/1.1"
    hosted = {}          # identifier -> code，模拟 PNVS 托管验证码

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n).decode("utf-8")
        path = urlparse(self.path).path

        # ---- Resend ----
        if path == "/emails":
            try:
                body = json.loads(raw)
            except ValueError:
                return self._json({"statusCode": 422,
                                   "name": "validation_error",
                                   "message": "invalid json"}, 422)
            SEEN.append({"kind": "resend", "body": body,
                         "auth": self.headers.get("Authorization"),
                         "idem": self.headers.get("Idempotency-Key")})
            if self.headers.get("Authorization") != f"Bearer {RESEND_KEY}":
                return self._json({"statusCode": 401,
                                   "name": "missing_api_key",
                                   "message": "Invalid API key"}, 401)
            if not body.get("to"):
                return self._json({"statusCode": 422,
                                   "name": "validation_error",
                                   "message": "to is required"}, 422)
            return self._json({"id": "mail-abc-123"})

        # ---- PNVS ----
        params = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True
                                              ).items()}
        action = params.get("Action", "")
        SEEN.append({"kind": "pnvs", "params": params})

        # 独立验签：签名错就按阿里云的形状返回 SignatureDoesNotMatch
        want = independent_sign(params, SK)
        if params.get("Signature") != want:
            return self._json({"Code": "SignatureDoesNotMatch",
                               "Message": f"expect {want}"}, 400)

        if action == "SendSmsVerifyCode":
            phone = params.get("PhoneNumber", "")
            if phone == "13800000999":       # 触发限频的号
                return self._json({"Code": R.PnvsProvider.ERR_FREQUENCY,
                                   "Message": "too frequent"}, 400)
            if phone == "13800000888":       # 触发当日上限
                return self._json({"Code": R.PnvsProvider.ERR_DAILY_LIMIT,
                                   "Message": "daily limit"}, 400)
            Handler.hosted[phone] = "654321"
            return self._json({"Code": "OK", "Message": "OK",
                               "Model": {"BizId": "biz-999"}})

        if action == "CheckSmsVerifyCode":
            phone = params.get("PhoneNumber", "")
            got = params.get("VerifyCode", "")
            if phone == "13800000777":
                # 关键陷阱：外层 OK 但内层 FAIL —— 只判外层会把码错当成成功
                return self._json({"Code": "OK", "Message": "OK",
                                   "Model": {"VerifyResult": "FAIL"}})
            if phone == "13800000666":
                return self._json({"Code": "OK", "Message": "OK",
                                   "Model": {"VerifyResult": "UNKNOWN"}})
            if phone == "13800000555":
                return self._json({"Code": "OK", "Message": "OK",
                                   "Model": {}})      # 缺 VerifyResult
            ok = Handler.hosted.get(phone) == got
            return self._json({"Code": "OK", "Message": "OK",
                               "Model": {"VerifyResult":
                                         "PASS" if ok else "FAIL"}})

        self._json({"Code": "InvalidAction", "Message": action}, 400)


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# 阿里云官方文档《V2版本RPC风格API请求的请求体与签名机制》的固定参数示例。
# https://help.aliyun.com/zh/sdk/product-overview/rpc-mechanism
#
# 这是唯一的**权威**基准：前面所有"签名自洽"的断言都只能证明我们内部一致，
# 证明不了与阿里云一致。有了它，SignatureDoesNotMatch 类问题才能在本地就暴露。
OFFICIAL_VECTOR = {
    "params": {
        "AccessKeyId": "testid",
        "Action": "DescribeDedicatedHosts",
        "Format": "JSON",
        "RegionId": "cn-beijing",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": "edb2b34af0af9a6d14deaf7c1a5315eb",
        "SignatureVersion": "1.0",
        "Timestamp": "2023-03-13T08:34:30Z",
        "Version": "2014-05-26",
    },
    "secret": "testsecret",
    "method": "GET",
    "expected": "9NaGiOspFP5UPcwX8Iwt2YJXXuk=",
}


def test_official_vector():
    section("[0] 阿里云官方测试向量（权威基准）")
    v = OFFICIAL_VECTOR
    got = R.sign(v["params"], v["secret"], v["method"])
    check("签名与官方文档给出的期望值逐字节一致",
          got == v["expected"], f"得到 {got}，期望 {v['expected']}")

    # 顺带钉住中间产物，回归时能直接看出是哪一环变了
    sts = R.build_string_to_sign(v["params"], v["method"])
    check("待签串以 GET&%2F& 开头", sts.startswith("GET&%2F&"), sts[:40])
    check("待签串里 Timestamp 的冒号被二次编码",
          "Timestamp%3D2023-03-13T08%253A34%253A30Z" in sts, sts[-90:])
    check("改动任一参数即偏离官方向量",
          R.sign({**v["params"], "RegionId": "cn-hangzhou"},
                 v["secret"], v["method"]) != v["expected"])
    check("换 HTTP 方法即偏离官方向量",
          R.sign(v["params"], v["secret"], "POST") != v["expected"])


def test_signature_rules():
    section("[1] RPC 签名规则")
    check("斜杠被编码", R.percent_encode("/") == "%2F")
    check("空格编码为 %20 而非 +", R.percent_encode("a b") == "a%20b")
    check("星号编码为 %2A", R.percent_encode("*") == "%2A")
    check("波浪线还原为 ~", R.percent_encode("~") == "~")
    check("冒号被编码",
          R.percent_encode("2016-02-23T12:46:24Z")
          == "2016-02-23T12%3A46%3A24Z")
    check("中文按 UTF-8 编码", R.percent_encode("测") == "%E6%B5%8B")

    sts = R.build_string_to_sign({"B": "2", "A": "1"}, "POST")
    check("待签串结构为 METHOD&%2F&<编码后查询串>",
          sts == "POST&%2F&A%3D1%26B%3D2", sts)
    check("Signature 不参与签名",
          R.build_string_to_sign({"A": "1", "Signature": "x"})
          == R.build_string_to_sign({"A": "1"}))
    check("None 值被剔除",
          R.build_string_to_sign({"A": "1", "B": None})
          == R.build_string_to_sign({"A": "1"}))

    s1 = R.sign({"A": "1"}, "sec")
    manual = base64.b64encode(hmac.new(
        b"sec&", R.build_string_to_sign({"A": "1"}).encode(),
        hashlib.sha1).digest()).decode()
    check("密钥补 & 后做 HMAC-SHA1", s1 == manual)
    nobar = base64.b64encode(hmac.new(
        b"sec", R.build_string_to_sign({"A": "1"}).encode(),
        hashlib.sha1).digest()).decode()
    check("不补 & 时签名不同（可捕获该类错误）", s1 != nobar)
    check("改参数签名变", s1 != R.sign({"A": "2"}, "sec"))
    check("改密钥签名变", s1 != R.sign({"A": "1"}, "sec2"))
    check("改方法签名变", s1 != R.sign({"A": "1"}, "sec", "GET"))
    check("与独立实现算出的签名一致",
          s1 == independent_sign({"A": "1"}, "sec"))


async def test_resend(base):
    section("[2] Resend 邮件通道")
    p = R.ResendProvider(api_key=RESEND_KEY, sender="noreply@example.com",
                         api_base=base)
    check("ready() 在凭据齐备时为真", p.ready() is True)

    mail_id, err = await p.send_code("email", "u@example.com", "123456")
    check("发信成功返回 id", mail_id == "mail-abc-123", f"{mail_id} {err}")
    last = [s for s in SEEN if s["kind"] == "resend"][-1]
    check("Bearer 认证头正确", last["auth"] == f"Bearer {RESEND_KEY}")
    check("带幂等键（重试不会重复发信）", bool(last["idem"]))
    check("收件人归一成列表", last["body"]["to"] == ["u@example.com"])
    check("发件人用配置值", last["body"]["from"] == "noreply@example.com")
    check("正文含验证码", "123456" in last["body"]["text"]
          and "123456" in last["body"]["html"])
    check("同时给 text 与 html",
          bool(last["body"]["text"]) and bool(last["body"]["html"]))
    check("主题含验证码", "123456" in last["body"]["subject"])

    mail_id, err = await p.send_code("phone", "13800000001", "123456")
    check("拒绝处理手机通道", mail_id is None and "不处理" in str(err), str(err))
    mail_id, err = await p.send_code("email", "u@example.com", None)
    check("邮箱通道必须传验证码（我们自管）",
          mail_id is None and "必须传入" in str(err), str(err))

    bad = R.ResendProvider(api_key="wrong", api_base=base)
    mail_id, err = await bad.send_code("email", "u@example.com", "1")
    check("错 API key 映射为错误",
          mail_id is None and "401" in str(err)
          and "missing_api_key" in str(err), str(err))

    nokey = R.ResendProvider(api_key="", api_base=base)
    check("未配置 key 时 ready() 为假", nokey.ready() is False)
    mail_id, err = await nokey.send_code("email", "u@example.com", "1")
    check("未配置 key 时不发请求且报错",
          mail_id is None and "RESEND_API_KEY" in str(err), str(err))

    dead = R.ResendProvider(api_key=RESEND_KEY, api_base="http://127.0.0.1:1")
    mail_id, err = await dead.send_code("email", "u@example.com", "1")
    check("网络异常被兜住", mail_id is None and "请求异常" in str(err), str(err))


def new_pnvs(base):
    return R.PnvsProvider(access_key_id=AK, access_key_secret=SK,
                          sign_name="系统签名", template_code="SMS_TPL_1",
                          api_url=base + "/")


async def test_pnvs_send(base):
    section("[3] PNVS 发码")
    p = new_pnvs(base)
    check("ready() 在凭据齐备时为真", p.ready() is True)

    biz, err = await p.send_code("phone", "13800000001", None)
    check("发码成功返回 BizId", biz == "biz-999", f"{biz} {err}")
    last = [s for s in SEEN if s["kind"] == "pnvs"][-1]["params"]
    check("mock 端独立验签通过（说明签名正确）", err is None)
    check("Action 正确", last["Action"] == "SendSmsVerifyCode")
    check("签名相关公共参数齐备",
          all(k in last for k in ("SignatureMethod", "SignatureVersion",
                                  "SignatureNonce", "Timestamp",
                                  "AccessKeyId", "Version")))
    check("SignName / TemplateCode 来自配置",
          last["SignName"] == "系统签名"
          and last["TemplateCode"] == "SMS_TPL_1")
    tp = json.loads(last["TemplateParam"])
    check("TemplateParam 用 ##code## 占位符（由阿里云填值）",
          tp.get("code") == "##code##", str(tp))
    check("CodeLength 与本地配置一致",
          last["CodeLength"] == str(providers.CODE_LENGTH))
    check("ValidTime 与本地 TTL 一致",
          last["ValidTime"] == str(providers.CODE_TTL))
    check("中文签名参与签名后仍验签通过", err is None)

    biz, err = await p.send_code("email", "u@example.com", None)
    check("拒绝处理邮箱通道", biz is None and "不处理" in str(err), str(err))
    biz, err = await p.send_code("phone", "13800000001", "123456")
    check("手机通道不接受调用方生成的验证码",
          biz is None and "托管" in str(err), str(err))

    # 供应商侧限频与当日上限要能识别
    biz, err = await p.send_code("phone", "13800000999", None)
    check("识别 FREQUENCY_FAIL", biz is None and "频繁" in str(err), str(err))
    biz, err = await p.send_code("phone", "13800000888", None)
    check("识别 BUSINESS_LIMIT_CONTROL",
          biz is None and "当日" in str(err), str(err))

    bad = R.PnvsProvider(access_key_id=AK, access_key_secret="wrong-secret",
                         sign_name="s", template_code="t", api_url=base + "/")
    biz, err = await bad.send_code("phone", "13800000001", None)
    check("错密钥被 mock 端判为 SignatureDoesNotMatch",
          biz is None and "SignatureDoesNotMatch" in str(err), str(err))

    p2 = R.PnvsProvider(access_key_id="", access_key_secret="",
                        sign_name="", template_code="", api_url=base + "/")
    check("凭据缺失时 ready() 为假", p2.ready() is False)
    biz, err = await p2.send_code("phone", "13800000001", None)
    check("凭据缺失时报错且不发请求", biz is None and err, str(err))


async def test_pnvs_check(base):
    section("[4] PNVS 校验：两层成功判据")
    p = new_pnvs(base)
    await p.send_code("phone", "13800000002", None)      # mock 会存 654321

    ok, err = await p.check_hosted("13800000002", "654321")
    check("正确验证码通过（Code=OK 且 VerifyResult=PASS）", ok is True, str(err))

    await p.send_code("phone", "13800000003", None)
    ok, err = await p.check_hosted("13800000003", "000000")
    check("错误验证码不通过", ok is False and err == "invalid_code", str(err))

    # 这条是本适配层最关键的断言
    ok, err = await p.check_hosted("13800000777", "whatever")
    check("外层 Code=OK 但 VerifyResult=FAIL 时判为失败"
          "（只判外层会把码错当成功）",
          ok is False and err == "invalid_code", f"ok={ok} err={err}")

    ok, err = await p.check_hosted("13800000666", "whatever")
    check("VerifyResult=UNKNOWN 映射为 code_expired",
          ok is False and err == "code_expired", str(err))

    ok, err = await p.check_hosted("13800000555", "whatever")
    check("响应缺 VerifyResult 时判为失败（拿不到判据不能算通过）",
          ok is False and err == "invalid_code", str(err))

    bad = R.PnvsProvider(access_key_id=AK, access_key_secret="wrong",
                         sign_name="s", template_code="t", api_url=base + "/")
    ok, err = await bad.check_hosted("13800000002", "654321")
    check("签名错时判为不通过（不能因外层错误就放行）",
          ok is False, str(err))


async def test_routing(base):
    section("[5] 通道分发与凭据回落")
    email_p = R.ResendProvider(api_key=RESEND_KEY, api_base=base)
    phone_p = new_pnvs(base)
    rp = R.RoutingProvider(email=email_p, phone=phone_p)

    mid, err = await rp.send_code("email", "r@example.com", "111111")
    check("邮箱走 Resend", mid == "mail-abc-123", f"{mid} {err}")
    biz, err = await rp.send_code("phone", "13800000004", None)
    check("手机走 PNVS", biz == "biz-999", f"{biz} {err}")
    ok, err = await rp.check_hosted("13800000004", "654321")
    check("check_hosted 只走手机通道", ok is True, str(err))
    check("calls 汇总两个通道", rp.calls == email_p.calls + phone_p.calls)
    check("真实通道不暴露验证码（peek_code 返回 None）",
          rp.peek_code("13800000004") is None)

    # 无凭据时回落到 mock：开发与 CI 不该因缺凭据而跑不起来
    saved = {k: os.environ.pop(k, None) for k in
             ("RESEND_API_KEY", "ALIYUN_ACCESS_KEY_ID",
              "ALIYUN_ACCESS_KEY_SECRET", "DYPNS_SIGN_NAME",
              "DYPNS_TEMPLATE_CODE")}
    try:
        chosen = R.from_env()
        check("无凭据时邮箱回落到 MockProvider",
              isinstance(chosen["email"], providers.MockProvider))
        check("无凭据时手机回落到 MockProvider",
              isinstance(chosen["phone"], providers.MockProvider))
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


async def run_all():
    PASS.clear(); FAIL.clear(); RESULTS.clear(); SEEN.clear()
    Handler.hosted.clear()
    srv, base = serve()
    try:
        # 前两段是纯同步（签名规则），后四段要 await（provider 已 async）
        sync_fns = {test_official_vector, test_signature_rules}
        for fn, args in ((test_official_vector, ()), (test_signature_rules, ()),
                         (test_resend, (base,)),
                         (test_pnvs_send, (base,)), (test_pnvs_check, (base,)),
                         (test_routing, (base,))):
            try:
                if fn in sync_fns:
                    fn(*args)
                else:
                    await fn(*args)
            except Exception as e:
                check(f"{fn.__name__} 整段异常", False,
                      f"{type(e).__name__}: {e}")
    finally:
        srv.shutdown()
    return {"results": list(RESULTS), "passed": len(PASS), "failed": len(FAIL),
            "failures": list(FAIL), "total": len(RESULTS)}


SABOTAGES = [
    ("只判外层 Code，不看 VerifyResult",
     "会把'验证码错误'当成'校验成功'——本适配层后果最严重的一处",
     lambda: _patch_method(R.PnvsProvider, "check_hosted", _check_outer_only)),
    ("百分号编码退化成默认 quote",
     "斜杠不编码、空格变 +，签名必然对不上",
     lambda: _patch(R, "percent_encode",
                    lambda s: __import__("urllib.parse", fromlist=["quote"])
                    .quote(str(s)))),
    ("签名密钥不补 &",
     "RPC v1.0 规定密钥尾部有 &，漏掉即 SignatureDoesNotMatch",
     lambda: _patch(R, "sign", _sign_no_amp)),
    ("待签串不排序",
     "规范化查询串必须按参数名排序",
     lambda: _patch(R, "build_string_to_sign", _sts_unsorted)),
    ("Resend 不带 Bearer 前缀",
     "认证头格式错，全部请求 401",
     lambda: _patch_method(R.ResendProvider, "send_code", _resend_no_bearer)),
    ("手机通道接受调用方生成的验证码",
     "PNVS 托管验证码，自己生成的码永远校验不过",
     lambda: _patch_method(R.PnvsProvider, "send_code", _pnvs_accept_code)),
]


def _patch(mod, name, fn):
    orig = getattr(mod, name)
    setattr(mod, name, fn)
    return lambda: setattr(mod, name, orig)


def _patch_method(cls, name, fn):
    orig = getattr(cls, name)
    setattr(cls, name, fn)
    return lambda: setattr(cls, name, orig)


async def _check_outer_only(self, identifier, code):
    body, err = await self._call("CheckSmsVerifyCode",
                           PhoneNumber=identifier, VerifyCode=str(code))
    if err:
        return False, "invalid_code"
    return True, None               # 破坏点：外层 OK 就算通过


def _sign_no_amp(params, secret, method="POST"):
    return base64.b64encode(hmac.new(
        secret.encode(), R.build_string_to_sign(params, method).encode(),
        hashlib.sha1).digest()).decode()


def _sts_unsorted(params, method="POST"):
    items = [(k, v) for k, v in params.items()
             if k != "Signature" and v is not None]
    canonical = "&".join(f"{R.percent_encode(k)}={R.percent_encode(v)}"
                         for k, v in items)
    return f"{method}&{R.percent_encode('/')}&{R.percent_encode(canonical)}"


async def _resend_no_bearer(self, provider, identifier, code):
    if provider != "email":
        return None, f"ResendProvider 不处理 {provider} 通道"
    if not self.api_key:
        return None, "未配置 RESEND_API_KEY"
    if code is None:
        return None, "邮箱通道必须传入验证码（我们自管，不由供应商生成）"
    self.calls += 1
    status, body = await R._http(
        "POST", f"{self.api_base}/emails",
        headers={"Authorization": self.api_key,      # 破坏点：缺 Bearer
                 "Content-Type": "application/json"},
        body=json.dumps({"from": self.sender, "to": [identifier],
                         "subject": f"验证码：{code}", "text": code,
                         "html": code}))
    if status >= 400:
        return None, f"HTTP {status} {body.get('name', '')}"
    return body.get("id"), None


async def _pnvs_accept_code(self, provider, identifier, code):
    if provider != "phone":
        return None, f"PnvsProvider 不处理 {provider} 通道"
    self.calls += 1
    body, err = await self._call(
        "SendSmsVerifyCode", PhoneNumber=identifier,
        SignName=self.sign_name, TemplateCode=self.template_code,
        TemplateParam=json.dumps({"code": code or "##code##", "min": "10"}),
        CodeLength=str(providers.CODE_LENGTH), CodeType="1",
        Interval="60", ValidTime=str(providers.CODE_TTL))
    if err:
        return None, self._friendly(err)
    return ((body or {}).get("Model") or {}).get("BizId"), None


def run_negative():
    import contextlib
    import io
    print("反向验证：逐个植入破坏点，确认供应商自检能抓出来\n")
    all_caught = True
    for name, why, apply_fn in SABOTAGES:
        restore = apply_fn()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                s = anyio.run(run_all)
        finally:
            restore()
        caught = s["failed"] > 0
        all_caught = all_caught and caught
        print(f"  [{'抓到' if caught else '漏掉'}] {name}")
        print(f"         理由：{why}")
        print(f"         失败 {s['failed']} 项"
              + (f"，例如：{'; '.join(s['failures'][:2])}" if caught else ""))
    with contextlib.redirect_stdout(io.StringIO()):
        healthy = anyio.run(run_all)
    print(f"\n  恢复后：失败 {healthy['failed']} 项（应为 0）")
    effective = all_caught and healthy["failed"] == 0
    print("\n  结论：" + ("每个破坏点都被抓到，且恢复后全绿——自检有约束力"
                          if effective else "有破坏点未被抓到，需修正自检"))
    return 0 if effective else 1


def main():
    try:
        from pnvs_console import setup_console
        setup_console()
    except ImportError:
        pass
    if "--negative" in sys.argv:
        return run_negative()
    s = anyio.run(run_all)
    print(f"\n通过 {s['passed']} / {s['total']}，失败 {s['failed']}")
    if s["failed"]:
        print("失败项：" + "; ".join(s["failures"][:10]))
    else:
        print("签名规则、请求体、响应解析、错误码映射、两层判据、通道分发"
              "均已验证（对本地 mock，未出公网、未花钱）。")
        print("尚未验证：真实收到邮件 / 短信 —— 需服务器上的真实凭据，"
              "见 deploy/验收清单.md。")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
