# -*- coding: utf-8 -*-
"""认证服务契约测试（第 0 步产出）。

对着 auth_contract.py 校验**任意一个** base URL 上的实现，不含实现代码。

    python 契约测试.py --base http://127.0.0.1:8000     # 打真实服务
    python 契约测试.py --reference                       # 打内置参考实现
    python 契约测试.py --unimplemented                   # 打空服务（应全红）
    python 契约测试.py --negative                        # 反向验证
    python 契约测试.py --json

设计要点：**测行为，不只测形状。** 只断言"字段在不在"是弱测试——把 token 写死成
常量也能过。所以关键断言都是行为性的：踢设备后必须 401、同一码不能用两次、
限频必须在调用 provider 之前发生、并发注册只能建一个账号。

第 0 步的验收标准不是"全绿"，而是三件事同时成立：
    --unimplemented  全红      （证明测试真的在检查）
    --reference      全绿      （证明契约自身可满足、不矛盾）
    --negative       弄坏就红  （证明断言有约束力）
只有第二条会随实现推进而变化，前后两条永远该成立。
"""

import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

import auth_contract as C

PASS, FAIL = [], []
RESULTS = []
_SECTION = ""


def section(name):
    global _SECTION
    _SECTION = name
    print(f"\n{name}")


def check(name, cond, detail=""):
    """cond 可为值或无参函数；函数抛异常算失败，不中断整轮。"""
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


def uniq(n=[0]):
    """单调递增计数器，用于造互不冲突的邮箱/号码/IP。"""
    n[0] += 1
    return n[0]


# ---------------------------------------------------------------- HTTP 工具
def call(base, key, body=None, token=None, headers=None, ip=None, **path_args):
    """发一次请求，返回 (状态码, 响应字典)。网络/解析失败也收敛成状态码。

    ip 用于伪造客户端地址。默认每次调用换一个，避免"同 IP 5 次/60s"这条限频
    把无关用例连带打成 429——只有专门测 IP 限频的用例才固定同一个 ip。
    """
    spec = C.ENDPOINTS[key]
    target = C.url(base, key, **path_args)
    hdrs = {"Content-Type": "application/json",
            C.CLIENT_IP_HEADER: ip or f"10.0.{uniq() % 250}.{uniq() % 250}"}
    if token:
        hdrs.update(C.bearer(token))
    if headers:
        hdrs.update(headers)
    data = json.dumps(body or {}).encode() if spec["method"] != "GET" else None
    req = urllib.request.Request(target, data=data, headers=hdrs,
                                 method=spec["method"])
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw or b"{}")
            except ValueError:
                return resp.status, {"__raw__": raw[:200].decode(errors="replace")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"__raw__": raw[:200].decode(errors="replace")}
    except Exception as e:
        return 0, {"__error__": repr(e)[:200]}


def shape_ok(obj, shape):
    """字段齐备且类型匹配。"""
    if not isinstance(obj, dict):
        return False
    for field, typ in shape.items():
        if field not in obj:
            return False
        if not isinstance(obj[field], typ):
            return False
    return True


def test_send(base):
    section("[1] 发码端点")
    st, body = call(base, "otp", {"email": f"u{uniq()}@example.com"})
    check("POST /otp 返回 200", st == 200, f"{st} {body}")
    check("/otp 响应含 retryAfter:int",
          shape_ok(body, C.ENDPOINTS["otp"]["responses"][200]), str(body))

    st, body = call(base, "otp", {"email": "not-an-email"})
    check("非法邮箱返回 400", st == 400, f"{st} {body}")
    # 必须连状态码一起断言：只查 error 字段的话，任何 4xx 错误体都能混过去
    check("400 响应含 error",
          st == 400 and isinstance(body.get("error"), str), f"{st} {body}")

    st, body = call(base, "sms_send", {"phone": f"138{uniq():08d}"})
    check("POST /sms/send 返回 200", st == 200, f"{st} {body}")
    check("/sms/send 响应含 retryAfter",
          shape_ok(body, C.ENDPOINTS["sms_send"]["responses"][200]), str(body))

    st, body = call(base, "sms_send", {"phone": "12345"})
    check("非法手机号返回 400", st == 400, f"{st} {body}")

    # 缺必填字段
    st, body = call(base, "otp", {})
    check("缺 email 返回 400", st == 400, f"{st} {body}")

    # 归一化：三种写法应命中同一限频桶 -> 第二次起被 429
    phone = f"139{uniq():08d}"
    st1, _ = call(base, "sms_send", {"phone": phone})
    st2, b2 = call(base, "sms_send", {"phone": f"+86 {phone}"})
    check("归一化生效：+86 前缀视为同一号码（第二次 429）",
          st1 == 200 and st2 == 429, f"{st1} then {st2} {b2}")
    st3, _ = call(base, "sms_send", {"phone": f"86{phone}"})
    check("归一化生效：86 前缀视为同一号码", st3 == 429, str(st3))

    # 同邮箱限频
    em = f"r{uniq()}@example.com"
    sa, _ = call(base, "otp", {"email": em})
    sb, bb = call(base, "otp", {"email": em})
    check("同邮箱 60s 内重发返回 429", sa == 200 and sb == 429,
          f"{sa} then {sb}")
    check("429 响应含 retryAfter",
          isinstance(bb.get("retryAfter"), int), str(bb))
    # Gmail 点号归一
    sc, _ = call(base, "otp", {"email": "a.b.c@gmail.com"})
    sd, _ = call(base, "otp", {"email": "abc@gmail.com"})
    check("Gmail 点号归一化（第二次 429）", sc == 200 and sd == 429,
          f"{sc} then {sd}")


def test_verify_and_login(base):
    section("[2] 校验与登录")
    email = f"v{uniq()}@example.com"
    call(base, "otp", {"email": email})

    st, body = call(base, "verify_email",
                    {"email": email, "code": "000000",
                     "deviceKey": "dk-1", "platform": "win32"})
    check("错误验证码返回 401", st == 401, f"{st} {body}")

    code = fetch_code(base, email)
    check("能取到测试验证码（mock/参考实现暴露）", bool(code), str(code))

    st, body = call(base, "verify_email",
                    {"email": email, "code": code or "x",
                     "deviceKey": "dk-1", "platform": "win32"})
    check("正确验证码返回 200", st == 200, f"{st} {body}")
    is_new = bool(body.get("isNewUser"))
    check("新用户返回 tempToken 而非 token",
          is_new and shape_ok(body, C.VERIFY_OK_NEW), str(body))
    # 必须限定在 200 下判断：空服务返回 404 时也"没有 token"，会假绿
    check("新用户不直接下发正式 token",
          st == 200 and "token" not in body, f"{st} {body}")

    # 同一验证码不可重放
    st2, b2 = call(base, "verify_email",
                   {"email": email, "code": code or "x",
                    "deviceKey": "dk-1", "platform": "win32"})
    check("验证码命中即删，不可重放", st2 == 401, f"{st2} {b2}")

    # /complete 建号
    st, body = call(base, "complete",
                    {"tempToken": body.get("tempToken", ""),
                     "deviceKey": "dk-1", "platform": "win32"})
    check("/complete 返回 200", st == 200, f"{st} {body}")
    check("/complete 响应含 token 与 user",
          shape_ok(body, C.ENDPOINTS["complete"]["responses"][200]), str(body))
    check("user 字段形状符合契约",
          shape_ok(body.get("user") or {}, C.USER_SHAPE),
          str(body.get("user")))
    token = body.get("token")

    st, b = call(base, "complete", {"tempToken": "bogus-temp",
                                    "deviceKey": "d", "platform": "win32"})
    check("无效 tempToken 返回 401", st == 401, f"{st} {b}")

    # 老用户复登录另起一段测（见 test_returning_user）：同邮箱 60s 内只允许
    # 发 1 次码，靠"给同一邮箱重发"来测必被 429，那是测试写错而非实现有问题。
    return token, email


def test_returning_user(base):
    section("[2b] 老用户复登录（登录注册同入口）")
    phone = f"136{uniq():08d}"
    call(base, "sms_send", {"phone": phone})
    code = fetch_code(base, phone)
    st, b = call(base, "verify_phone",
                 {"phone": phone, "code": code or "x",
                  "deviceKey": "dk-r1", "platform": "win32"})
    st, b = call(base, "complete",
                 {"tempToken": b.get("tempToken", ""),
                  "deviceKey": "dk-r1", "platform": "win32"})
    if st != 200:
        check("前置：手机号注册成功", False, f"{st} {b}")
        return None, None
    check("前置：手机号注册成功", True)

    # 同号第二次登录需要新码，但同号 60s 限频。用测试钩子清限频计数——
    # 刻意不用 sweep：sweep 不重置活跃窗口，那是它该有的行为。
    reset_limits(base)
    call(base, "sms_send", {"phone": phone})
    code2 = fetch_code(base, phone)
    check("清限频计数后可再次发码", bool(code2), str(code2))
    st, body = call(base, "verify_phone",
                    {"phone": phone, "code": code2 or "x",
                     "deviceKey": "dk-r1", "platform": "win32"})
    check("老用户直接返回 token（登录注册同入口）",
          st == 200 and shape_ok(body, C.VERIFY_OK_EXISTING), f"{st} {body}")
    check("老用户不返回 isNewUser=true",
          st == 200 and not body.get("isNewUser"), f"{st} {body}")
    return body.get("token"), phone


TEST_HOOK_PATH = "/__test__/code"


def fetch_code(base, identifier):
    """取出刚发的验证码。

    生产实现绝不该暴露这个；它只存在于 mock provider 与参考实现里，用于让
    契约测试能走完"发码 → 校验"闭环。真实链路（第 5/6 步）改成人工收信/收码。
    """
    target = f"{base.rstrip('/')}{TEST_HOOK_PATH}?id={urllib.parse.quote(identifier)}"
    try:
        with urllib.request.urlopen(target, timeout=10) as resp:
            return (json.loads(resp.read() or b"{}") or {}).get("code")
    except Exception:
        return None


def test_session_and_revoke(base, token, email):
    section("[3] 登录态与设备撤销（R5）")
    st, body = call(base, "me", token=token)
    check("GET /me 带 token 返回 200", st == 200, f"{st} {body}")
    check("/me 含 user 与 identities",
          shape_ok(body, C.ENDPOINTS["me"]["responses"][200]), str(body))
    check("identities 里能看到注册用的邮箱",
          any(email in json.dumps(i, ensure_ascii=False)
              for i in (body.get("identities") or [])), str(body.get("identities")))

    st, body = call(base, "me")
    check("GET /me 不带 token 返回 401", st == 401, f"{st} {body}")
    st, body = call(base, "me", token="bogus-token-xxx")
    check("GET /me 用伪造 token 返回 401", st == 401, f"{st} {body}")
    st, body = call(base, "me", headers={C.AUTH_HEADER: token or ""})
    check("缺 Bearer scheme 返回 401", st == 401, f"{st} {body}")

    # 第二台设备登录：同样受同号 60s 限频，先清限频计数再发码
    reset_limits(base)
    is_phone = bool(email) and "@" not in str(email)
    send_key = "sms_send" if is_phone else "otp"
    field = "phone" if is_phone else "email"
    call(base, send_key, {field: email})
    c2 = fetch_code(base, email)
    st, body = call(base, "verify_phone" if is_phone else "verify_email",
                    {field: email, "code": c2 or "x",
                     "deviceKey": "dk-2", "platform": "darwin"})
    token2 = body.get("token")
    check("第二台设备可登录同一账号", st == 200 and bool(token2), f"{st} {body}")

    st, body = call(base, "sessions_list", token=token)
    devices = body.get("devices") or []
    check("GET /sessions 返回 200", st == 200, f"{st} {body}")
    check("设备列表含两台", len(devices) >= 2, str(devices))
    check("设备项形状符合契约",
          devices and all(shape_ok(d, C.DEVICE_SHAPE) for d in devices),
          str(devices))     # devices 为空时 all() 恒真，必须先要求非空
    check("恰有一台标记 current",
          sum(1 for d in devices if d.get("current")) == 1, str(devices))

    # 踢掉第二台：核心行为断言
    target = next((d for d in devices if not d.get("current")), None)
    st, body = call(base, "session_revoke", token=token,
                    id=(target or {}).get("id", "x"))
    check("DELETE /sessions/{id} 返回 200", st == 200, f"{st} {body}")

    st, body = call(base, "me", token=token2)
    check("被踢设备的 token 立即 401（即时撤销，不用 JWT 的核心理由）",
          st == 401, f"{st} {body}")
    st, body = call(base, "me", token=token)
    check("当前设备不受影响，仍 200", st == 200, f"{st} {body}")

    st, body = call(base, "session_revoke", token=token, id="no-such-device")
    # 要求是契约定义的 404 错误体，而不是"服务整体没实现"的 404
    check("撤销不存在的设备返回 404（且为契约错误体）",
          st == 404 and body.get("error") in C.ERRORS, f"{st} {body}")

    # 登出
    st, body = call(base, "logout", token=token)
    check("POST /logout 返回 200", st == 200, f"{st} {body}")
    st, body = call(base, "me", token=token)
    check("登出后原 token 立即 401", st == 401, f"{st} {body}")


def test_concurrency_and_limits(base):
    section("[4] 并发与限频边界")

    # 并发注册同一邮箱：只能建出一个账号
    email = f"cc{uniq()}@example.com"
    call(base, "otp", {"email": email})
    code = fetch_code(base, email)
    results = []
    lock = threading.Lock()

    def race(i):
        st, b = call(base, "verify_email",
                     {"email": email, "code": code or "x",
                      "deviceKey": f"dk-race-{i}", "platform": "win32"})
        with lock:
            results.append((st, b))

    ts = [threading.Thread(target=race, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    wins = [b for st, b in results if st == 200]
    check("并发校验同一码：只有一个成功（命中即删）",
          len(wins) == 1, f"{len(wins)} 个成功 / 共 {len(results)}")

    # 完成注册后再并发调 /complete 用同一 tempToken
    tt = (wins[0].get("tempToken") if wins else None)
    if tt:
        done = []
        def race2(i):
            st, b = call(base, "complete",
                         {"tempToken": tt, "deviceKey": f"dk-c{i}",
                          "platform": "win32"})
            with lock:
                done.append(st)
        ts = [threading.Thread(target=race2, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("并发 /complete 同一 tempToken：只建一个账号",
              done.count(200) == 1, f"200 出现 {done.count(200)} 次 {done}")
    else:
        check("并发 /complete 同一 tempToken：只建一个账号", False,
              "上一步没拿到 tempToken")

    # 校验侧限频（R7）：6 位码 100 万种，不限次即可穷举
    em = f"bf{uniq()}@example.com"
    call(base, "otp", {"email": em})
    codes = [f"{i:06d}" for i in range(9)]
    sts = [call(base, "verify_email",
                {"email": em, "code": c, "deviceKey": "dk-bf",
                 "platform": "win32"})[0] for c in codes]
    check("校验侧限频生效：连续错码最终返回 429",
          429 in sts, f"状态序列 {sts}")
    check("限频前的错码返回 401 而非 429",
          sts[0] == 401, f"首个状态 {sts[0]}")

    # 同 IP 发码限频（R6）：这里必须固定同一个 IP，否则测不到这条限频
    same_ip = f"203.0.113.{uniq() % 250}"
    sts = [call(base, "otp", {"email": f"ip{uniq()}@example.com"},
                ip=same_ip)[0] for _ in range(8)]
    check("同 IP 发码限频生效（最终 429）", 429 in sts, f"状态序列 {sts}")
    check("不同 IP 不受他人配额影响",
          call(base, "otp", {"email": f"ip{uniq()}@example.com"},
               ip=f"198.51.100.{uniq() % 250}")[0] == 200)


def test_provider_order(base):
    section("[5] 限频必须发生在调用供应商之前")
    # 文档强调：撞供应商的闸时钱已经花了。用 mock 的调用计数来断言。
    em = f"po{uniq()}@example.com"
    st1, _ = call(base, "otp", {"email": em})
    before = provider_calls(base)
    st2, _ = call(base, "otp", {"email": em})       # 应被自己的限频挡掉
    after = provider_calls(base)
    check("首次发码确实调用了 provider", st1 == 200, str(st1))
    check("被限频的请求返回 429", st2 == 429, str(st2))
    # 必须先确认计数真的动过：否则未实现的服务恒为 0，0 == 0 会假绿
    check("被限频时 provider 调用次数未增加（省钱的关键）",
          before is not None and before > 0 and after == before,
          f"before={before} after={after}")


def provider_calls(base):
    """读 mock provider 的调用计数。仅 mock/参考实现提供。"""
    try:
        with urllib.request.urlopen(
                f"{base.rstrip('/')}/__test__/provider_calls", timeout=10) as r:
            return (json.loads(r.read() or b"{}") or {}).get("count")
    except Exception:
        return None


def test_cleanup(base):
    section("[6] 过期数据清理")
    # 方案文档：send_quota 窗口、email_codes 过期、sessions 过期都没有 TTL
    # 兜底机制，漏清不会报错，只会慢慢积垢——所以必须由验收来盯。
    counts = table_counts(base)
    if counts is None:
        check("能读到行数统计（脚手架钩子）", False, "未提供 /__test__/counts")
        return
    check("能读到行数统计（脚手架钩子）", True)

    ems = [f"cl{uniq()}@example.com" for _ in range(3)]
    for e in ems:
        call(base, "otp", {"email": e})
    mid = table_counts(base)
    check("发码后 codes 表有行", mid and mid.get("codes", 0) >= 3, str(mid))

    # 命中即删
    code = fetch_code(base, ems[0])
    call(base, "verify_email", {"email": ems[0], "code": code or "x",
                                "deviceKey": "dk-cl", "platform": "win32"})
    after = table_counts(base)
    check("命中即删：codes 行数减少",
          after and mid and after.get("codes", 99) < mid.get("codes", 0),
          f"{mid} -> {after}")

    # 过期清理
    swept = sweep(base)
    check("提供清理入口（定时任务应调用同一逻辑）", swept is not None,
          "未提供 /__test__/sweep")
    if swept is not None:
        final = table_counts(base)
        # final 为 None 时必须红：拿不到统计就等于没验证，不能算通过
        check("清理后过期 codes 行不残留",
              final is not None and final.get("expired_codes", -1) == 0,
              str(final))
        check("清理后过期 quota 窗口不残留",
              final is not None and final.get("expired_quota", -1) == 0,
              str(final))
        check("清理后过期 sessions 不残留",
              final is not None and final.get("expired_sessions", -1) == 0,
              str(final))


def table_counts(base):
    try:
        with urllib.request.urlopen(
                f"{base.rstrip('/')}/__test__/counts", timeout=10) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def reset_limits(base):
    """清限频计数。仅测试钩子；生产不该有这个端点。

    刻意不复用 sweep：sweep 不重置活跃窗口（正确行为），测试要连续登录多次，
    才需要这个显式重置。
    """
    try:
        req = urllib.request.Request(
            f"{base.rstrip('/')}/__test__/reset_limits", data=b"{}",
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def sweep(base):
    try:
        req = urllib.request.Request(
            f"{base.rstrip('/')}/__test__/sweep", data=b"{}", method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def run_all(base):
    """对 base 跑完整套契约测试。每段单独兜异常，一段崩掉不影响后面。"""
    PASS.clear(); FAIL.clear(); RESULTS.clear()
    token = email = None
    try:
        test_send(base)
    except Exception as e:
        check("test_send 整段异常", False, f"{type(e).__name__}: {e}")
    try:
        token, email = test_verify_and_login(base)
    except Exception as e:
        check("test_verify_and_login 整段异常", False, f"{type(e).__name__}: {e}")
    try:
        # 设备相关的用例接着老用户那条身份走：它的限频窗口可被 sweep 清掉，
        # 邮箱那条在同一轮里已用掉 60s 配额。
        rtoken, rident = test_returning_user(base)
        if rtoken:
            token, email = rtoken, rident
    except Exception as e:
        check("test_returning_user 整段异常", False, f"{type(e).__name__}: {e}")
    try:
        test_session_and_revoke(base, token, email)
    except Exception as e:
        check("test_session_and_revoke 整段异常", False, f"{type(e).__name__}: {e}")
    try:
        test_concurrency_and_limits(base)
    except Exception as e:
        check("test_concurrency_and_limits 整段异常", False,
              f"{type(e).__name__}: {e}")
    try:
        test_provider_order(base)
    except Exception as e:
        check("test_provider_order 整段异常", False, f"{type(e).__name__}: {e}")
    try:
        test_cleanup(base)
    except Exception as e:
        check("test_cleanup 整段异常", False, f"{type(e).__name__}: {e}")
    return {"results": list(RESULTS), "passed": len(PASS), "failed": len(FAIL),
            "failures": list(FAIL), "total": len(RESULTS)}


SABOTAGES = [
    ("不做手机号归一化",
     "归一化若在限频之后，同一个人能注册多个账号、也能绕过限频",
     lambda ref: _patch(ref, "norm_phone", lambda raw: (
         str(raw or "").strip() or None))),
    ("撤销设备不生效",
     "R5 要求即时撤销；这是不用 JWT 的核心理由，必须能被测出",
     lambda ref: _patch_state(ref, "revoke_noop", True)),
    ("验证码校验后不删除",
     "命中即删是防重放的唯一保障",
     lambda ref: _patch_state(ref, "keep_code", True)),
    ("限频放到调用 provider 之后",
     "撞供应商的闸时钱已经花了",
     lambda ref: _patch_state(ref, "limit_after_provider", True)),
    ("新用户直接下发正式 token",
     "两段式注册被跳过，/complete 形同虚设",
     lambda ref: _patch_state(ref, "skip_temp", True)),
]


def _patch(ref, name, fn):
    original = getattr(ref, name)
    setattr(ref, name, fn)
    return lambda: setattr(ref, name, original)


def _patch_state(ref, flag, value):
    original = getattr(ref, "SABOTAGE", {}).copy()
    ref.SABOTAGE[flag] = value
    def restore():
        ref.SABOTAGE.clear()
        ref.SABOTAGE.update(original)
    return restore


def run_negative(ref, *_ignored):
    """反向验证：逐个植入破坏点，每个都必须让契约测试转红。

    用多个破坏点而不是一个：60 条断言分属不同关注点，单一破坏点只能证明其中
    一簇有约束力。每个破坏点都对应方案文档里一处明确要求。
    """
    print("反向验证：逐个植入破坏点，确认契约测试能抓出来\n")
    rows, all_caught = [], True

    for name, why, apply_fn in SABOTAGES:
        srv, base = ref.serve(mode="full")
        restore = apply_fn(ref)
        try:
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                s = run_all(base)
        finally:
            restore()
            srv.shutdown()
        caught = s["failed"] > 0
        all_caught = all_caught and caught
        rows.append((name, why, s["failed"], caught, s["failures"][:3]))
        mark = "抓到" if caught else "漏掉"
        print(f"  [{mark}] {name}")
        print(f"         理由：{why}")
        print(f"         失败 {s['failed']} 项"
              + (f"，例如：{'; '.join(s['failures'][:3])}" if caught else ""))

    # 恢复后必须全绿，否则说明破坏点没清干净
    srv, base = ref.serve(mode="full")
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        healthy = run_all(base)
    srv.shutdown()

    print(f"\n  恢复后：失败 {healthy['failed']} 项"
          f"（应为 0，否则破坏点没清干净）")
    effective = all_caught and healthy["failed"] == 0
    print("\n  结论：" + ("每个破坏点都被抓到，且恢复后全绿——契约测试有约束力"
                          if effective else
                          "有破坏点未被抓到或未清干净，需修正测试"))
    return 0 if effective else 1


def main():
    try:
        import os
        sys.path.insert(0, os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        from pnvs_console import setup_console
        setup_console()
    except ImportError:
        pass

    args = sys.argv[1:]
    base = None
    if "--base" in args:
        base = args[args.index("--base") + 1]

    srv = None
    if "--reference" in args or "--unimplemented" in args or "--negative" in args:
        import 参考实现 as ref
        mode = ("empty" if "--unimplemented" in args else "full")
        srv, base = ref.serve(mode=mode)

    if not base:
        print(__doc__)
        return 2

    if "--negative" in args:
        import 参考实现 as ref
        summary = run_negative(ref, base, srv)
        return summary

    summary = run_all(base)
    if srv:
        srv.shutdown()

    if "--json" in args:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if summary["failed"] else 0

    print(f"\n通过 {summary['passed']} / {summary['total']}，"
          f"失败 {summary['failed']}")
    if summary["failed"]:
        print("失败项：" + "; ".join(summary["failures"][:12])
              + (" …" if len(summary["failures"]) > 12 else ""))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
