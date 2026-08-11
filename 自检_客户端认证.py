# -*- coding: utf-8 -*-
"""客户端 AuthStore / AuthManager 自检（第 7 步可本地验证的部分）。

**能在这里验**：凭证加解密与落盘、device_key 跨重装稳定、登出保留 device_key、
钥匙串不可用时降级且告警、云端各响应的处理（含 401 清凭证）、网络异常兜底。

**验不了**（源码包只含 gateway/，缺 _appdata/_logging/_sockets/ai/router/session
五个同级模块，装不起真的 Gateway）：
    /auth/* 路由注册是否正确
    --auth-endpoint 为空时是否零回归
这两条要等完整的 src/psi_agent/ 源码树，本文件不假装覆盖。

为了让被测代码能 import，这里给 psi_agent._appdata 打一个**最小桩**——只提供
resolve_appdata_root 一个函数。桩只替代路径解析，不替代任何被测逻辑。

    python 自检_客户端认证.py
    python 自检_客户端认证.py --negative
"""

import json
import os
import sys
import tempfile
import types

GATEWAY_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "psi-agent-src", "src")

PASS, FAIL = [], []
RESULTS = []
_SECTION = ""
_LOGS = []


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


def _install_appdata_stub():
    """给 psi_agent._appdata 打最小桩。只有真模块缺失时才装。"""
    if os.path.isdir(GATEWAY_SRC):
        sys.path.insert(0, os.path.abspath(GATEWAY_SRC))
    try:
        import psi_agent._appdata          # noqa: F401
        return False
    except Exception:
        pass
    pkg = sys.modules.get("psi_agent")
    if pkg is None:
        pkg = types.ModuleType("psi_agent")
        pkg.__path__ = [os.path.join(os.path.abspath(GATEWAY_SRC), "psi_agent")]
        sys.modules["psi_agent"] = pkg
    mod = types.ModuleType("psi_agent._appdata")

    async def resolve_appdata_root(appdata_root: str = "") -> str:
        return appdata_root or tempfile.mkdtemp()

    mod.resolve_appdata_root = resolve_appdata_root
    sys.modules["psi_agent._appdata"] = mod
    return True


STUBBED = _install_appdata_stub()

import anyio                                            # noqa: E402
from loguru import logger                               # noqa: E402

# 捕获 warning：降级必须告警，这条要能被断言
logger.remove()
logger.add(lambda m: _LOGS.append(m), level="DEBUG")


def _load_by_path(mod_name, filename):
    """按文件路径直接加载被测模块。

    不能走 `import psi_agent.gateway._auth_manager` —— 那会先执行
    gateway/__init__.py，它 import 了源码包里缺失的 _logging / _sockets /
    ai / router / session。按路径加载可以只装载被测文件本身，
    被测代码一行没改。
    """
    import importlib.util
    path = os.path.join(os.path.abspath(GATEWAY_SRC), "psi_agent", "gateway",
                        filename)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_store_mod = _load_by_path("_auth_store_under_test", "_auth_store.py")
AuthStore = _store_mod.AuthStore
# _auth_manager 内部 `from psi_agent.gateway._auth_store import AuthStore`，
# 预置该模块名，令它拿到同一个类（否则会再次触发包 __init__）。
sys.modules.setdefault("psi_agent.gateway", types.ModuleType("psi_agent.gateway"))
sys.modules["psi_agent.gateway._auth_store"] = _store_mod
AuthManager = _load_by_path("_auth_manager_under_test",
                            "_auth_manager.py").AuthManager


class FakeKeyring:
    """可用的假钥匙串。"""

    def __init__(self):
        self.d = {}

    def get_password(self, service, user):
        return self.d.get((service, user))

    def set_password(self, service, user, pw):
        self.d[(service, user)] = pw


class BrokenKeyring:
    """不可用的钥匙串：模拟 Linux 无 Secret Service / CI 无 D-Bus。"""

    def get_password(self, service, user):
        raise RuntimeError("no secret service")

    def set_password(self, service, user, pw):
        raise RuntimeError("no secret service")


def test_store():
    section("[1] 凭证落盘（R4：不明文落盘）")

    async def body():
        tmp = tempfile.mkdtemp()
        st = await AuthStore.from_appdata(tmp, keyring_mod=FakeKeyring())
        path = os.path.join(tmp, "auth.enc.json")

        dk1 = await st.device_key()
        check("首次生成 device_key", bool(dk1) and len(dk1) >= 16, str(dk1))
        check("device_key 重复读取稳定（重装不刷新设备）",
              await st.device_key() == dk1)

        await st.save_token("secret-token-abc")
        check("token 可往返读回", await st.load_token() == "secret-token-abc")

        raw = open(path, encoding="utf-8").read()
        check("磁盘上没有 token 明文（R4 的核心）",
              "secret-token-abc" not in raw, raw[:120])
        check("文件标记 enc=true", json.loads(raw).get("enc") is True)
        check("文件带版本号（便于将来换 AES-GCM）",
              json.loads(raw).get("v") == 1)

        await st.clear_token()
        check("登出清空 token", await st.load_token() == "")
        check("登出保留 device_key（否则重登会被当成新设备）",
              await st.device_key() == dk1)

        # 损坏文件不应崩
        open(path, "w", encoding="utf-8").write("{not json")
        st2 = await AuthStore.from_appdata(tmp, keyring_mod=FakeKeyring())
        check("凭证文件损坏时视为未登录而非崩溃",
              await st2.load_token() == "")

        # 换了密钥（钥匙串被重置 / 换机器）：必须识别出来并视为未登录。
        # 异或本身没有完整性保证，只会解出乱码；靠校验和才能发现。
        # 若不发现，客户端会拿垃圾 token 去请求，界面却一直显示"已登录"。
        st3 = await AuthStore.from_appdata(tmp, keyring_mod=FakeKeyring())
        await st3.save_token("tok-1")
        st4 = await AuthStore.from_appdata(tmp, keyring_mod=FakeKeyring())
        got = await st4.load_token()
        check("钥匙串密钥变化时识别出来并返回空（不返回乱码）",
              got == "", repr(got))

    anyio.run(body)


def test_keyring_degrade():
    section("[2] 钥匙串不可用时降级且告警（不静默）")

    async def body():
        _LOGS.clear()
        tmp = tempfile.mkdtemp()
        st = await AuthStore.from_appdata(tmp, keyring_mod=BrokenKeyring())
        await st.save_token("plain-token-xyz")
        raw = open(os.path.join(tmp, "auth.enc.json"), encoding="utf-8").read()

        check("降级后仍能工作（不让客户端起不来）",
              await st.load_token() == "plain-token-xyz")
        check("降级时文件标记 enc=false（便于排查）",
              json.loads(raw).get("enc") is False)
        check("降级时 encrypted 属性为 False（供界面自曝）",
              st.encrypted is False)
        text = "".join(_LOGS)
        check("降级有明确 warning，不静默",
              "明文" in text and "WARNING" in text, text[:150])

    anyio.run(body)


import threading                                          # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import urlparse                          # noqa: E402

CLOUD = {"seen": [], "revoked": False, "unreachable": False}


class CloudHandler(BaseHTTPRequestHandler):
    """站在云端认证服务的位置，形状与 psi-agent-auth 的契约一致。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _auth_ok(self):
        h = self.headers.get("Authorization") or ""
        return h.startswith("Bearer ") and not CLOUD["revoked"]

    def _dispatch(self, method):
        path = urlparse(self.path).path.replace("/api/auth", "", 1)
        body = self._read()
        CLOUD["seen"].append({"m": method, "p": path, "b": body,
                              "auth": self.headers.get("Authorization")})
        if path in ("/sms/send", "/otp"):
            return self._json({"retryAfter": 60})
        if path in ("/verify/phone", "/verify/email"):
            if body.get("code") == "000000":
                return self._json({"error": "invalid_code"}, 401)
            if body.get("code") == "old":       # 老用户直接给 token
                return self._json({"token": "cloud-token-1",
                                   "user": {"id": "u1"}})
            return self._json({"tempToken": "tt-1", "isNewUser": True})
        if path == "/complete":
            if body.get("tempToken") != "tt-1":
                return self._json({"error": "temp_token_invalid"}, 401)
            return self._json({"token": "cloud-token-2", "user": {"id": "u2"}})
        if not self._auth_ok():
            return self._json({"error": "unauthorized"}, 401)
        if path == "/me":
            return self._json({"user": {"id": "u2"}, "identities": []})
        if path == "/sessions":
            return self._json({"devices": [{"id": "d1", "current": True}]})
        if path.startswith("/sessions/"):
            return self._json({"ok": True})
        if path == "/logout":
            return self._json({"ok": True})
        self._json({"error": "not_found"}, 404)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


def serve_cloud():
    """必须用 ThreadingHTTPServer：aiohttp 会保持 keep-alive 连接，
    单线程 HTTPServer 会被第一条连接占住，后续请求永远等不到响应（会挂死）。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), CloudHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_manager():
    section("[3] AuthManager 与云端交互")
    srv, base = serve_cloud()
    CLOUD["seen"].clear()
    CLOUD["revoked"] = False

    async def body():
        tmp = tempfile.mkdtemp()
        mgr = await AuthManager.create(base, appdata_root=tmp,
                                       platform="win32")
        try:
            st = mgr.status()
            check("初始未登录", st["loggedIn"] is False, str(st))
            check("status 带 deviceKey 与 platform",
                  bool(st["deviceKey"]) and st["platform"] == "win32")
            check("status 不泄露 token", "token" not in json.dumps(st).lower()
                  or "cloud-token" not in json.dumps(st))

            code, res = await mgr.send_code(email="u@example.com")
            check("发码走 /otp 并回 retryAfter",
                  code == 200 and res.get("retryAfter") == 60, f"{code} {res}")
            check("请求打到正确路径",
                  CLOUD["seen"][-1]["p"] == "/otp", str(CLOUD["seen"][-1]))

            code, res = await mgr.send_code(phone="13800000001")
            check("手机号走 /sms/send", CLOUD["seen"][-1]["p"] == "/sms/send")
            code, res = await mgr.send_code()
            check("手机号与邮箱都缺时前置拦截", code == 400, f"{code} {res}")

            code, res = await mgr.verify(email="u@example.com", code="000000")
            check("错验证码返回 401 且不落 token",
                  code == 401 and mgr.status()["loggedIn"] is False,
                  f"{code} {res}")

            code, res = await mgr.verify(email="u@example.com", code="123456")
            check("新用户拿到 tempToken", res.get("tempToken") == "tt-1")
            check("新用户此时仍未登录", mgr.status()["loggedIn"] is False)
            sent = CLOUD["seen"][-1]["b"]
            check("verify 带上 deviceKey 与 platform",
                  bool(sent.get("deviceKey")) and sent.get("platform") == "win32",
                  str(sent))

            code, res = await mgr.complete(temp_token="tt-1")
            check("/complete 换到正式 token", code == 200 and res.get("token"))
            check("完成后为已登录", mgr.status()["loggedIn"] is True)

            code, res = await mgr.me()
            check("/me 带 Bearer 头",
                  code == 200
                  and CLOUD["seen"][-1]["auth"] == "Bearer cloud-token-2",
                  str(CLOUD["seen"][-1]["auth"]))

            code, res = await mgr.list_devices()
            check("/sessions 返回设备列表",
                  code == 200 and res.get("devices"), f"{code} {res}")
            code, res = await mgr.revoke_device("d1")
            check("撤销设备成功", code == 200 and res.get("ok") is True)
            code, res = await mgr.revoke_device("")
            check("空 device_id 前置拦截", code == 400)
        finally:
            await mgr.aclose()
            srv.shutdown()

    anyio.run(body)


def test_persistence_and_401():
    section("[4] 跨重启保持（R3）与 401 清凭证")
    srv, base = serve_cloud()
    CLOUD["revoked"] = False

    async def body():
        tmp = tempfile.mkdtemp()
        mgr = await AuthManager.create(base, appdata_root=tmp)
        try:
            await mgr.verify(email="u@example.com", code="old")
            check("老用户直接登录成功", mgr.status()["loggedIn"] is True)
            dk = mgr.status()["deviceKey"]
        finally:
            await mgr.aclose()

        # 模拟客户端重启：新建 manager，从磁盘恢复
        mgr2 = await AuthManager.create(base, appdata_root=tmp)
        try:
            check("重启后仍为登录态（R3）", mgr2.status()["loggedIn"] is True)
            check("重启后 device_key 不变", mgr2.status()["deviceKey"] == dk)

            # 云端撤销该设备后，下一次请求应 401 并清本地
            CLOUD["revoked"] = True
            code, _res = await mgr2.me()
            check("云端撤销后 /me 返回 401", code == 401, str(code))
            check("401 后本机凭证被清（不再假装已登录）",
                  mgr2.status()["loggedIn"] is False)

            mgr3 = await AuthManager.create(base, appdata_root=tmp)
            try:
                check("重启后也不会复活已失效凭证",
                      mgr3.status()["loggedIn"] is False)
                check("device_key 仍保留", mgr3.status()["deviceKey"] == dk)
            finally:
                await mgr3.aclose()
        finally:
            await mgr2.aclose()
            srv.shutdown()

    anyio.run(body)


def test_failure_paths():
    section("[5] 失败路径")

    async def body():
        tmp = tempfile.mkdtemp()

        # endpoint 为空：不该发请求
        mgr = await AuthManager.create("", appdata_root=tmp)
        try:
            code, res = await mgr.send_code(email="u@example.com")
            check("endpoint 未配置时不发请求",
                  code == 0 and "not_configured" in str(res), f"{code} {res}")
            # endpoint 为空时，endpoint 检查先于 token 检查触发 —— 这个顺序是
            # 对的：没配置服务地址，谈不上"未登录"。
            code, res = await mgr.me()
            check("endpoint 未配置时 /me 也不打网络",
                  code == 0 and "not_configured" in str(res), f"{code} {res}")
        finally:
            await mgr.aclose()

        # 配了 endpoint 但未登录：应在本地就判 401，不打网络
        srv0, base0 = serve_cloud()
        CLOUD["seen"].clear()
        mgr0 = await AuthManager.create(base0, appdata_root=tempfile.mkdtemp())
        try:
            n_before = len(CLOUD["seen"])
            code, res = await mgr0.me()
            check("未登录调 /me 本地直接 401",
                  code == 401 and res.get("error") == "unauthorized",
                  f"{code} {res}")
            check("未登录时不向云端发请求（省一次往返）",
                  len(CLOUD["seen"]) == n_before,
                  f"{n_before} -> {len(CLOUD['seen'])}")
        finally:
            await mgr0.aclose()
            srv0.shutdown()

        # 云端不可达
        mgr2 = await AuthManager.create("http://127.0.0.1:1", appdata_root=tmp)
        try:
            code, res = await mgr2.send_code(email="u@example.com")
            check("云端不可达收敛成 (0, upstream_unreachable)",
                  code == 0 and res.get("error") == "upstream_unreachable",
                  f"{code} {res}")
        finally:
            await mgr2.aclose()

        # 登出时云端不可达：本机也要清
        srv, base = serve_cloud()
        CLOUD["revoked"] = False
        mgr3 = await AuthManager.create(base, appdata_root=tempfile.mkdtemp())
        try:
            await mgr3.verify(email="u@example.com", code="old")
            check("前置：已登录", mgr3.status()["loggedIn"] is True)
            srv.shutdown()          # 云端下线
            mgr3.endpoint = "http://127.0.0.1:1"
            code, _res = await mgr3.logout()
            check("云端不可达时登出仍清本机（否则界面骗人）",
                  mgr3.status()["loggedIn"] is False)
            check("登出对调用方仍报成功", code == 200, str(code))
        finally:
            await mgr3.aclose()

    anyio.run(body)


def run_all():
    PASS.clear(); FAIL.clear(); RESULTS.clear()
    for fn in (test_store, test_keyring_degrade, test_manager,
               test_persistence_and_401, test_failure_paths):
        try:
            fn()
        except Exception as e:
            check(f"{fn.__name__} 整段异常", False, f"{type(e).__name__}: {e}")
    return {"results": list(RESULTS), "passed": len(PASS), "failed": len(FAIL),
            "failures": list(FAIL), "total": len(RESULTS)}


SABOTAGES = [
    ("token 明文落盘",
     "R4 要求不明文落盘；库/文件被读走即等于凭证泄露",
     lambda: _patch_method(AuthStore, "save_token", _save_plain)),
    ("登出连 device_key 一起清",
     "重新登录会被云端当成新设备，设备列表越用越脏",
     lambda: _patch_method(AuthStore, "clear_token", _clear_all)),
    ("device_key 每次都重新生成",
     "重装即刷出新设备，UNIQUE(user_id, device_key) 形同虚设",
     lambda: _patch_method(AuthStore, "device_key", _dk_random)),
    ("401 不清本机凭证",
     "云端已撤销，界面却仍显示已登录",
     lambda: _patch_method(AuthManager, "_on_response", _noop_401)),
    ("verify 不带 deviceKey",
     "云端无法把会话绑到设备，R5 的设备列表与撤销都失效",
     lambda: _patch_method(AuthManager, "verify", _verify_no_dk)),
    ("钥匙串失败时静默降级",
     "用户以为凭证加密了，其实是明文",
     lambda: _patch_method(AuthStore, "_secret_key", _silent_degrade)),
]


def _patch_method(cls, name, fn):
    orig = getattr(cls, name)
    setattr(cls, name, fn)
    return lambda: setattr(cls, name, orig)


async def _save_plain(self, token):
    data = await self._read_raw()
    data["token"] = token
    data["enc"] = False
    data["v"] = 1
    await self._write_raw(data)


async def _clear_all(self):
    await self._write_raw({})


async def _dk_random(self):
    import secrets as _s
    data = await self._read_raw()
    fresh = _s.token_urlsafe(24)
    data["device_key"] = fresh
    await self._write_raw(data)
    return fresh


async def _noop_401(self, status):
    return None


async def _verify_no_dk(self, *, code, phone="", email=""):
    if not code:
        return 400, {"error": "code_required"}
    if phone:
        payload, path = {"phone": phone}, "/verify/phone"
    elif email:
        payload, path = {"email": email}, "/verify/email"
    else:
        return 400, {"error": "phone_or_email_required"}
    payload["code"] = code          # 破坏点：不带 deviceKey / platform
    status, body = await self._call("POST", path, payload)
    if status == 200 and body.get("token"):
        await self._adopt_token(str(body["token"]))
    return status, body


def _silent_degrade(self):
    self._encrypted = False
    return b""                      # 破坏点：不打 warning


def run_negative():
    import contextlib
    import io
    print("反向验证：逐个植入破坏点，确认客户端自检能抓出来\n")
    all_caught = True
    for name, why, apply_fn in SABOTAGES:
        restore = apply_fn()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                s = run_all()
        finally:
            restore()
        caught = s["failed"] > 0
        all_caught = all_caught and caught
        print(f"  [{'抓到' if caught else '漏掉'}] {name}")
        print(f"         理由：{why}")
        print(f"         失败 {s['failed']} 项"
              + (f"，例如：{'; '.join(s['failures'][:2])}" if caught else ""))
    with contextlib.redirect_stdout(io.StringIO()):
        healthy = run_all()
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
    if STUBBED:
        print("提示：psi_agent._appdata 用了最小桩（源码包只含 gateway/）。"
              "桩只替代路径解析，不替代被测逻辑。")
    if "--negative" in sys.argv:
        return run_negative()
    s = run_all()
    print(f"\n通过 {s['passed']} / {s['total']}，失败 {s['failed']}")
    if s["failed"]:
        print("失败项：" + "; ".join(s["failures"][:10]))
    else:
        print("凭证加密落盘、device_key 稳定、降级告警、云端交互、"
              "跨重启保持、401 清凭证、失败兜底均已验证。")
        print("路由注册与 --auth-endpoint 空值零回归见 自检_路由注册.py"
              "（用真实 create_app 实测，非本文件覆盖范围）。")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
