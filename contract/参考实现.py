# -*- coding: utf-8 -*-
"""契约参考实现（第 0 步产出，一次性脚手架，不是最终服务）。

存在的唯一目的：证明 auth_contract.py 的契约**自身可满足、不自相矛盾**。
第 1 步起会用真正的服务（SQLite + FastAPI）替换它，届时本文件可删。

刻意用内存字典而非 SQLite —— 契约测试不该依赖存储选型。真实实现的并发与
唯一约束靠数据库主键保证（见方案文档），那是第 1 步的验收内容，不在这里。

serve(mode="full")   完整实现，契约测试应全绿
serve(mode="empty")  只回 404，契约测试应全红（证明测试真的在检查）
"""

import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import auth_contract as C

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

# 反向验证用的破坏开关。正常运行时全空；契约测试的 --negative 会逐个打开，
# 确认每个破坏都能被测出来。生产实现里不该有这个东西。
SABOTAGE = {}


def norm_phone(raw):
    """去空格/横线/+、去 86 前缀。归一化必须在限频与入库之前。"""
    s = re.sub(r"[\s\-+]", "", str(raw or ""))
    if s.startswith("86"):
        s = s[2:]
    return s if PHONE_RE.match(s) else None


def norm_email(raw):
    """小写；Gmail 去点号。"""
    s = str(raw or "").strip().lower()
    if s.count("@") != 1 or " " in s:
        return None
    local, _, domain = s.partition("@")
    if not local or "." not in domain or domain.startswith(".") \
            or domain.endswith("."):
        return None
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
    return f"{local}@{domain}"


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.users = {}          # uid -> user dict
        self.identities = {}     # (provider, identifier) -> uid
        self.devices = {}        # did -> {uid, key, platform, ...}
        self.sessions = {}       # token -> {uid, did, revoked}
        self.codes = {}          # identifier -> {code, expires, attempts}
        self.temp = {}           # tempToken -> {provider, identifier, expires}
        self.send_log = {}       # bucket -> [ts]
        self.verify_log = {}     # identifier -> [ts]
        self.provider_calls = 0

    def hit(self, log, key, window, limit):
        """限频计数。返回 (是否放行, 还需等待秒数)。"""
        now = time.time()
        with self.lock:
            hist = [t for t in log.get(key, []) if now - t < window]
            log[key] = hist
            if len(hist) >= limit:
                return False, int(window - (now - min(hist))) + 1
            hist.append(now)
            return True, 0


    def sweep(self):
        """清掉过期的验证码 / 限频窗口 / 会话。

        方案文档强调：这几张表都没有 TTH 兜底机制，漏清不会报错，只会慢慢积垢。
        真实实现里由"写入时顺手删本 key 过期行 + 低频定时任务清全表"两层组成，
        定时任务应当调用与此同一份逻辑。
        """
        now = time.time()
        with self.lock:
            for k in [k for k, v in self.codes.items() if v["expires"] < now]:
                del self.codes[k]
            for k in [k for k, v in self.temp.items() if v["expires"] < now]:
                del self.temp[k]
            for k in [k for k, v in self.sessions.items()
                      if v["expires"] < now]:
                del self.sessions[k]
            for log, window in ((self.send_log, 60), (self.verify_log, 300)):
                for k in list(log):
                    hist = [t for t in log[k] if now - t < window]
                    if hist:
                        log[k] = hist
                    else:
                        del log[k]
        return self.counts()

    def reset_limits(self):
        """清空限频计数。**仅测试钩子**，生产绝不可暴露。

        与 sweep() 刻意分开：sweep 只清"窗口已完全过期"的桶，不会重置活跃限频
        ——那是正确行为，清理任务若能重置活跃限频，就等于开了绕过限频的后门。
        测试需要连续走多次登录，才需要这个显式重置。
        """
        with self.lock:
            self.send_log.clear()
            self.verify_log.clear()
        return {"ok": True}

    def counts(self):
        """行数统计，含"过期但仍残留"的行数——清理是否到位靠这几个数判断。"""
        now = time.time()
        with self.lock:
            return {
                "codes": len(self.codes),
                "temp": len(self.temp),
                "sessions": len(self.sessions),
                "users": len(self.users),
                "devices": len(self.devices),
                "expired_codes": sum(1 for v in self.codes.values()
                                     if v["expires"] < now),
                "expired_temp": sum(1 for v in self.temp.values()
                                    if v["expires"] < now),
                "expired_sessions": sum(1 for v in self.sessions.values()
                                        if v["expires"] < now),
                "expired_quota": sum(
                    1 for log, w in ((self.send_log, 60),
                                     (self.verify_log, 300))
                    for ts in log.values()
                    if all(now - t >= w for t in ts) and ts),
            }


def err(code):
    """按错误码表返回 (状态码, 响应体)。"""
    status, _msg = C.ERRORS[code]
    return status, {"error": code}


class Ref:
    """契约的参考实现。方法名对应 ENDPOINTS 的 key。"""

    def __init__(self, state):
        self.s = state

    # ---- 发码 ----
    def _send(self, provider, identifier, ip):
        s = self.s

        def call_provider():
            with s.lock:
                s.provider_calls += 1
                code = "".join(secrets.choice("0123456789")
                               for _ in range(C.EMAIL_CODE["length"]))
                s.codes[identifier] = {
                    "code": code,
                    "expires": time.time() + C.EMAIL_CODE["ttl_seconds"],
                    "attempts": 0, "provider": provider}

        # 破坏点：把 provider 调用提到限频之前（撞供应商的闸时钱已经花了）
        if SABOTAGE.get("limit_after_provider"):
            call_provider()

        lim = C.RATE_LIMITS["send_per_identifier"]
        ok, wait = s.hit(s.send_log, f"id:{identifier}", lim["window"],
                         lim["limit"])
        if not ok:
            st, body = err("rate_limited")
            body["retryAfter"] = wait
            return st, body
        lim = C.RATE_LIMITS["send_per_ip"]
        ok, wait = s.hit(s.send_log, f"ip:{ip}", lim["window"], lim["limit"])
        if not ok:
            st, body = err("rate_limited")
            body["retryAfter"] = wait
            return st, body

        # 限频通过后才调 provider —— 顺序即契约（省钱的关键）
        if not SABOTAGE.get("limit_after_provider"):
            call_provider()
        return 200, {"retryAfter": C.RATE_LIMITS["send_per_identifier"]["window"]}

    def sms_send(self, body, ip, token):
        phone = norm_phone(body.get("phone"))
        if not phone:
            return err("invalid_phone")
        return self._send("phone", phone, ip)

    def otp(self, body, ip, token):
        email = norm_email(body.get("email"))
        if not email:
            return err("invalid_email")
        return self._send("email", email, ip)

    # ---- 校验 ----
    def _verify(self, provider, identifier, code, device_key, platform):
        s = self.s
        if not identifier:
            return err("invalid_email" if provider == "email"
                       else "invalid_phone")
        if not code or not device_key or not platform:
            return err("invalid_code")

        lim = C.RATE_LIMITS["verify_per_identifier"]
        ok, _ = s.hit(s.verify_log, identifier, lim["window"], lim["limit"])
        if not ok:
            return err("rate_limited")

        with s.lock:
            rec = s.codes.get(identifier)
            if not rec or rec["expires"] < time.time():
                s.codes.pop(identifier, None)
                return err("code_expired")
            if rec["code"] != str(code).strip():
                rec["attempts"] += 1
                if rec["attempts"] >= C.EMAIL_CODE["max_attempts"]:
                    s.codes.pop(identifier, None)
                return err("invalid_code")
            if not SABOTAGE.get("keep_code"):
                s.codes.pop(identifier, None)    # 命中即删，防重放

            uid = s.identities.get((provider, identifier))
            if uid:
                token = self._issue(uid, device_key, platform)
                return 200, {"token": token, "user": s.users[uid]}
            # 破坏点：跳过两段式，直接建号发正式 token
            if SABOTAGE.get("skip_temp"):
                uid = f"user-{len(s.users) + 1}"
                s.users[uid] = {"id": uid, "displayName": None,
                                "avatarUrl": None, "createdAt": _now()}
                s.identities[(provider, identifier)] = uid
                token = self._issue(uid, device_key, platform)
                return 200, {"token": token, "user": s.users[uid]}
            tt = secrets.token_urlsafe(24)
            s.temp[tt] = {"provider": provider, "identifier": identifier,
                          "expires": time.time()
                          + C.TEMP_TOKEN_TTL_MINUTES * 60}
            return 200, {"tempToken": tt, "isNewUser": True}

    def verify_phone(self, body, ip, token):
        return self._verify("phone", norm_phone(body.get("phone")),
                            body.get("code"), body.get("deviceKey"),
                            body.get("platform"))

    def verify_email(self, body, ip, token):
        return self._verify("email", norm_email(body.get("email")),
                            body.get("code"), body.get("deviceKey"),
                            body.get("platform"))

    # ---- 建号与会话 ----
    def _issue(self, uid, device_key, platform):
        """签发 token。真实实现只存哈希，这里为脚手架从简，但形状一致。"""
        s = self.s
        did = next((d for d, v in s.devices.items()
                    if v["uid"] == uid and v["key"] == device_key), None)
        if did is None:
            did = f"dev-{len(s.devices) + 1}"
            s.devices[did] = {"uid": uid, "key": device_key,
                              "platform": platform, "name": None,
                              "createdAt": _now(), "lastSeenAt": None}
        token = secrets.token_urlsafe(32)
        s.sessions[token] = {"uid": uid, "did": did, "revoked": False,
                             "expires": time.time()
                             + C.TOKEN_ABSOLUTE_TTL_DAYS * 86400}
        return token

    def complete(self, body, ip, token):
        s = self.s
        tt = body.get("tempToken")
        device_key, platform = body.get("deviceKey"), body.get("platform")
        if not device_key or not platform:
            return err("invalid_code")
        with s.lock:
            rec = s.temp.pop(tt, None)      # pop：同一 tempToken 只能用一次
            if not rec or rec["expires"] < time.time():
                return err("temp_token_invalid")
            key = (rec["provider"], rec["identifier"])
            uid = s.identities.get(key)
            if uid is None:
                uid = f"user-{len(s.users) + 1}"
                s.users[uid] = {"id": uid,
                                "displayName": body.get("displayName"),
                                "avatarUrl": None, "createdAt": _now()}
                s.identities[key] = uid     # 唯一约束由此保证
            tok = self._issue(uid, device_key, platform)
            return 200, {"token": tok, "user": s.users[uid]}

    def _auth(self, token):
        s = self.s
        with s.lock:
            sess = s.sessions.get(token or "")
            if not sess or sess["revoked"] or sess["expires"] < time.time():
                return None
            sess_dev = s.devices.get(sess["did"])
            if sess_dev:
                sess_dev["lastSeenAt"] = _now()
            return sess

    def me(self, body, ip, token):
        sess = self._auth(token)
        if not sess:
            return err("unauthorized")
        s = self.s
        idents = [{"provider": p, "identifier": i}
                  for (p, i), u in s.identities.items() if u == sess["uid"]]
        return 200, {"user": s.users[sess["uid"]], "identities": idents}

    def logout(self, body, ip, token):
        sess = self._auth(token)
        if not sess:
            return err("unauthorized")
        with self.s.lock:
            self.s.sessions[token]["revoked"] = True
        return 200, {"ok": True}

    def sessions_list(self, body, ip, token):
        sess = self._auth(token)
        if not sess:
            return err("unauthorized")
        s = self.s
        out = []
        for did, d in s.devices.items():
            if d["uid"] != sess["uid"]:
                continue
            # 该设备是否还有未撤销的会话
            alive = any(v["did"] == did and not v["revoked"]
                        for v in s.sessions.values())
            if not alive:
                continue
            out.append({"id": did, "platform": d["platform"],
                        "name": d["name"], "createdAt": d["createdAt"],
                        "lastSeenAt": d["lastSeenAt"],
                        "current": did == sess["did"]})
        return 200, {"devices": out}

    def session_revoke(self, body, ip, token, target_id=None):
        sess = self._auth(token)
        if not sess:
            return err("unauthorized")
        s = self.s
        with s.lock:
            dev = s.devices.get(target_id or "")
            if not dev or dev["uid"] != sess["uid"]:
                return err("not_found")
            n = 0
            for v in s.sessions.values():
                if v["did"] == target_id and not v["revoked"]:
                    if not SABOTAGE.get("revoke_noop"):
                        v["revoked"] = True     # 即时生效
                    n += 1
            if n == 0:
                return err("not_found")
        return 200, {"ok": True}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _match(method, path):
    """把 (method, path) 映射到 ENDPOINTS 的 key，返回 (key, 路径参数)。"""
    if not path.startswith(C.PREFIX):
        return None, None
    rest = path[len(C.PREFIX):]
    for key, spec in C.ENDPOINTS.items():
        if spec["method"] != method:
            continue
        tpl = spec["path"]
        if "{" not in tpl:
            if tpl == rest:
                return key, {}
            continue
        head, _, _tail = tpl.partition("{")
        if rest.startswith(head) and len(rest) > len(head):
            return key, {"id": rest[len(head):]}
    return None, None


def make_handler(state, mode):
    ref = Ref(state)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, obj, status):
            raw = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _token(self):
            h = self.headers.get(C.AUTH_HEADER) or ""
            prefix = C.AUTH_SCHEME + " "
            return h[len(prefix):] if h.startswith(prefix) else None

        def _handle(self, method):
            u = urlparse(self.path)

            # 测试钩子：仅脚手架提供，生产绝不暴露。
            # 放在 mode 检查之后——未实现的服务连钩子也不该有，否则契约测试
            # 会在空服务上假绿（清理类断言恰好因"表为空"而通过）。
            if mode != "empty":
                if u.path == "/__test__/code":
                    ident = (parse_qs(u.query).get("id") or [""])[0]
                    key = norm_email(ident) or norm_phone(ident) or ident
                    rec = state.codes.get(key)
                    return self._json({"code": rec["code"] if rec else None},
                                      200)
                if u.path == "/__test__/provider_calls":
                    return self._json({"count": state.provider_calls}, 200)
                if u.path == "/__test__/counts":
                    return self._json(state.counts(), 200)
                if u.path == "/__test__/sweep" and method == "POST":
                    return self._json(state.sweep(), 200)
                if u.path == "/__test__/reset_limits" and method == "POST":
                    return self._json(state.reset_limits(), 200)

            if mode == "empty":
                return self._json({"error": "not_implemented"}, 404)

            key, path_args = _match(method, u.path)
            if key is None:
                return self._json({"error": "not_found"}, 404)

            n = int(self.headers.get("Content-Length", 0) or 0)
            body = {}
            if n:
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._json({"error": "invalid_json"}, 400)

            fn = getattr(ref, key)
            # 跑在反代后面时 socket 地址是反代的，必须读转发头，
            # 否则所有用户共用一个限频桶。
            fwd = self.headers.get(C.CLIENT_IP_HEADER)
            ip = (fwd.split(",")[0].strip() if fwd
                  else self.client_address[0])
            try:
                if key == "session_revoke":
                    st, out = fn(body, ip, self._token(),
                                 target_id=path_args.get("id"))
                else:
                    st, out = fn(body, ip, self._token())
            except Exception as e:                  # noqa: BLE001
                return self._json({"error": "internal",
                                   "detail": repr(e)[:200]}, 500)
            self._json(out, st)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

    return H


def serve(mode="full", port=0):
    """起服务，返回 (server, base_url)。mode="empty" 时只回 404。"""
    state = State()
    # 必须 ThreadingHTTPServer：并发用例会留下 keep-alive 连接占住单线程，
    # 之后的请求（如 /__test__/sweep）会超时 —— 表现为偶发的"未提供该钩子"。
    # 这个坑在本项目已出现三次，每次都是单线程 HTTPServer + keep-alive。
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state, mode))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    srv, base = serve(port=8000)
    print(f"参考实现已启动：{base}{C.PREFIX}")
    print("这是第 0 步的一次性脚手架，仅用于验证契约可满足，不是最终服务。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
