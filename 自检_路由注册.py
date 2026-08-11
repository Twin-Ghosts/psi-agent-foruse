# -*- coding: utf-8 -*-
"""/auth/* 路由注册与零回归实测（第 7 步验收的另一半）。

用**真实的** create_app 建 aiohttp 应用，不打桩、不模拟路由表。验两件事：

1. ``--auth-endpoint`` 为空时 ``/auth/*`` **一条都不存在**，且其余路由数量与
   不带认证时完全一致 —— 这是改动能安全落地的前提，必须实测而非假设。
2. 配了 endpoint 时八条路由齐备、方法正确，且请求真的被转发到云端。

    python 自检_路由注册.py
    python 自检_路由注册.py --negative
"""

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
FULL_SRC = os.path.join(HERE, "..", "psi-agent-full", "src")
sys.path.insert(0, os.path.abspath(FULL_SRC))

PASS, FAIL = [], []
RESULTS = []
_SECTION = ""


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


from loguru import logger  # noqa: E402

logger.remove()          # 静音，免淹没自检输出

import anyio  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from psi_agent.gateway._ai_manager import AIManager  # noqa: E402
from psi_agent.gateway._auth_manager import AuthManager  # noqa: E402
from psi_agent.gateway._router_manager import RouterManager  # noqa: E402
from psi_agent.gateway._session_manager import SessionManager  # noqa: E402
from psi_agent.gateway._title_manager import TitleManager  # noqa: E402
from psi_agent.gateway.server import create_app  # noqa: E402

CLOUD = {"seen": []}


class CloudHandler(BaseHTTPRequestHandler):
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

    def _go(self, method):
        raw_path = urlparse(self.path).path
        # 同时接受带前缀与裸路径：前缀可配，mock 不该只认一种
        path = raw_path.replace("/api/auth", "", 1)
        if path.startswith("/auth/"):
            path = path[len("/auth"):]
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = {}
        if n:
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
        CLOUD["seen"].append({"m": method, "p": path, "b": body,
                              "raw_path": raw_path,
                              "auth": self.headers.get("Authorization")})
        if path in ("/sms/send", "/otp"):
            return self._json({"retryAfter": 60})
        if path.startswith("/verify/"):
            return self._json({"tempToken": "tt-1", "isNewUser": True})
        if path == "/complete":
            return self._json({"token": "tok-1", "user": {"id": "u1"}})
        if path == "/me":
            return self._json({"error": "unauthorized"}, 401)
        if path == "/sessions":
            return self._json({"error": "unauthorized"}, 401)
        if path.startswith("/sessions/"):
            return self._json({"error": "unauthorized"}, 401)
        if path == "/logout":
            return self._json({"error": "unauthorized"}, 401)
        self._json({"error": "not_found"}, 404)

    def do_GET(self):
        self._go("GET")

    def do_POST(self):
        self._go("POST")

    def do_DELETE(self):
        self._go("DELETE")


def serve_cloud():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), CloudHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


async def build_app(authm=None):
    """用真实 create_app 建应用。managers 用真实类型但不启动子进程。"""
    async with anyio.create_task_group() as tg:
        aim = AIManager(_prefix="selftest", _tg=tg)
        rm = RouterManager(_aim=aim, _prefix="selftest", _tg=tg)
        sm = SessionManager(_aim=aim, _rm=rm, _prefix="selftest", _tg=tg,
                            _default_agent="", _default_workspace=tempfile.mkdtemp(),
                            _appdata=tempfile.mkdtemp())
        app = await create_app(aim, sm, TitleManager(), rm=rm,
                               appdata=tempfile.mkdtemp(), authm=authm)
        tg.cancel_scope.cancel()
    return app


def routes_of(app):
    """列出 (方法, 路径) —— 直接读真实路由表，不是我自己记的清单。"""
    out = []
    for res in app.router.resources():
        path = getattr(res, "canonical", str(res))
        for route in res:
            out.append((route.method, path))
    return out


def test_zero_regression():
    section("[1] auth_endpoint 为空时零回归（必须实测）")

    async def body():
        app_off = await build_app(authm=None)
        routes_off = routes_of(app_off)
        auth_routes = [r for r in routes_off if r[1].startswith("/auth")]
        check("未配 endpoint 时 /auth/* 一条都不注册",
              not auth_routes, str(auth_routes))
        check("app 里不存 authm 键（不创建 manager）",
              "authm" not in app_off, "authm 被创建了")

        srv, base = serve_cloud()
        try:
            mgr = await AuthManager.create(base, appdata_root=tempfile.mkdtemp())
            try:
                app_on = await build_app(authm=mgr)
                routes_on = routes_of(app_on)
                # 按「路径」数而非「方法行」数：aiohttp 对每个 add_get 会自动
                # 附带一条 HEAD，方法行数会多出来，那不是我们多注册的。
                auth_paths = {r[1] for r in routes_on if r[1].startswith("/auth")}
                check("配了 endpoint 时注册 8 个 /auth 端点",
                      len(auth_paths) == 8, f"{len(auth_paths)} 个: {sorted(auth_paths)}")

                # 关键：除 /auth/* 外，两种配置的路由表必须完全一致
                non_auth_off = sorted(r for r in routes_off
                                      if not r[1].startswith("/auth"))
                non_auth_on = sorted(r for r in routes_on
                                     if not r[1].startswith("/auth"))
                check("其余路由与不带认证时完全一致（真正的零回归）",
                      non_auth_off == non_auth_on,
                      f"差异: {set(non_auth_on) ^ set(non_auth_off)}")
                check("非认证路由数量不为零（说明确实建起了完整应用）",
                      len(non_auth_off) > 30, str(len(non_auth_off)))
            finally:
                await mgr.aclose()
        finally:
            srv.shutdown()

    anyio.run(body)


def test_route_shapes():
    section("[2] 八条路由的方法与路径")

    async def body():
        srv, base = serve_cloud()
        try:
            mgr = await AuthManager.create(base, appdata_root=tempfile.mkdtemp())
            try:
                app = await build_app(authm=mgr)
                got = {(m, p) for m, p in routes_of(app) if p.startswith("/auth")}
                expected = {
                    ("GET", "/auth/status"),
                    ("POST", "/auth/send-code"),
                    ("POST", "/auth/verify"),
                    ("POST", "/auth/complete"),
                    ("GET", "/auth/me"),
                    ("POST", "/auth/logout"),
                    ("GET", "/auth/devices"),
                    ("DELETE", "/auth/devices/{device_id}"),
                }
                for item in sorted(expected):
                    check(f"{item[0]:6} {item[1]}", item in got, str(sorted(got)))
                # HEAD 由 aiohttp 为每个 GET 自动添加，不算多注册
                extra = {(m, p) for m, p in got - expected if m != "HEAD"}
                check("没有多注册路由（HEAD 由 aiohttp 自动附带，不计）",
                      not extra, f"多出: {sorted(extra)}")
            finally:
                await mgr.aclose()
        finally:
            srv.shutdown()

    anyio.run(body)


def test_end_to_end():
    section("[3] 经真实 aiohttp 应用打通到云端")

    async def body():
        srv, base = serve_cloud()
        CLOUD["seen"].clear()
        try:
            mgr = await AuthManager.create(base, appdata_root=tempfile.mkdtemp())
            app = await build_app(authm=mgr)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                r = await client.get("/auth/status")
                data = await r.json()
                check("GET /auth/status 返回 200", r.status == 200, str(r.status))
                check("status 含 loggedIn / deviceKey",
                      "loggedIn" in data and "deviceKey" in data, str(data))
                check("status 不含 token 字段", "token" not in data, str(data))

                r = await client.post("/auth/send-code",
                                      json={"email": "u@example.com"})
                check("POST /auth/send-code 转发成功", r.status == 200, str(r.status))
                check("请求真的打到云端 /otp",
                      CLOUD["seen"] and CLOUD["seen"][-1]["p"] == "/otp",
                      str(CLOUD["seen"][-1:]))

                r = await client.post("/auth/send-code", json={})
                check("手机号与邮箱都缺时返回 400", r.status == 400, str(r.status))

                # 必须区分两种 400：非法 JSON 与"字段缺失"要给不同信息，
                # 否则前端无法告诉用户到底哪里错了。只断言状态码的话，
                # "把非法 JSON 当成空对象"这种退化不会被发现。
                r = await client.post("/auth/send-code", data=b"not json")
                err = (await r.json()).get("error", "")
                check("非法 JSON 返回 400 而非 500", r.status == 400, str(r.status))
                check("非法 JSON 的错误信息指明是 JSON 问题",
                      "JSON" in err or "json" in err, f"得到 {err!r}")

                r = await client.post("/auth/verify",
                                      json={"email": "u@example.com", "code": "123456"})
                body_json = await r.json()
                check("POST /auth/verify 拿到 tempToken",
                      r.status == 200 and body_json.get("tempToken") == "tt-1",
                      f"{r.status} {body_json}")
                sent = CLOUD["seen"][-1]["b"]
                check("verify 转发时带上 deviceKey 与 platform",
                      bool(sent.get("deviceKey")) and bool(sent.get("platform")),
                      str(sent))

                r = await client.post("/auth/complete", json={"tempToken": "tt-1"})
                check("POST /auth/complete 登录成功", r.status == 200, str(r.status))
                r = await client.get("/auth/status")
                check("登录后 status.loggedIn 为真",
                      (await r.json()).get("loggedIn") is True)

                # 云端回 401 时要如实转发，不能掩饰成 200
                r = await client.get("/auth/me")
                check("云端 401 被如实转发", r.status == 401, str(r.status))
                r = await client.get("/auth/status")
                check("401 后本机登录态已清",
                      (await r.json()).get("loggedIn") is False)

                r = await client.get("/auth/devices")
                check("GET /auth/devices 可达", r.status in (401, 200), str(r.status))
                r = await client.delete("/auth/devices/d1")
                check("DELETE /auth/devices/{id} 可达",
                      r.status in (401, 200, 400), str(r.status))
            finally:
                await client.close()
                await mgr.aclose()
        finally:
            srv.shutdown()

    anyio.run(body)


def test_prefix_configurable():
    section("[5] 云端路由前缀可配（线上存在两种形态）")
    # 本仓库契约是 /api/auth/*；但服务器上的 psi-cloud 直接在根路径提供
    # /otp、/verify/email。前缀写死会导致对着其中一种部署全部 404，
    # 而客户端一旦发布就改不动了。
    import importlib

    src = open(os.path.join(os.path.abspath(FULL_SRC), "psi_agent", "gateway",
                            "_auth_manager.py"), encoding="utf-8").read()
    check("前缀不是写死的常量拼接",
          "self.prefix" in src and "_resolve_prefix" in src,
          "前缀被写死，换部署形态就全部 404")
    check("可用 PSI_AUTH_PREFIX 覆盖", "PSI_AUTH_PREFIX" in src)
    check("status() 暴露 prefix（404 时第一个该看的地方）",
          '"prefix": self.prefix' in src)

    async def body():
        # 关键：断言**实际请求到的路径**，不是 status() 里报出来的字段。
        # 只查报告值的话，"把 URL 拼接写死但字段仍在"这种退化不会被发现
        # （第一版就是这么漏掉的）。
        cases = [(None, "/api/auth/otp"), ("", "/otp"), ("/auth/", "/auth/otp")]
        for env, expect_path in cases:
            if env is None:
                os.environ.pop("PSI_AUTH_PREFIX", None)
            else:
                os.environ["PSI_AUTH_PREFIX"] = env
            for m in [k for k in sys.modules if "auth_manager" in k]:
                del sys.modules[m]
            importlib.invalidate_caches()
            from psi_agent.gateway._auth_manager import AuthManager as AM

            srv, base = serve_cloud()
            CLOUD["seen"].clear()
            mgr = await AM.create(base, appdata_root=tempfile.mkdtemp())
            try:
                await mgr.send_code(email="p@example.com")
                got = CLOUD["seen"][-1]["raw_path"] if CLOUD["seen"] else "(无请求)"
                check(f"PSI_AUTH_PREFIX={env!r} 时实际请求 {expect_path}",
                      got == expect_path, f"实际请求了 {got}")
            finally:
                await mgr.aclose()
                srv.shutdown()
        os.environ.pop("PSI_AUTH_PREFIX", None)

    anyio.run(body)


def test_cloud_down():
    section("[4] 云端不可达时的表现")

    async def body():
        mgr = await AuthManager.create("http://127.0.0.1:1",
                                       appdata_root=tempfile.mkdtemp())
        app = await build_app(authm=mgr)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.post("/auth/send-code", json={"email": "u@example.com"})
            check("云端不可达返回 502（不是 0，也不掩饰成 200）",
                  r.status == 502, str(r.status))
            data = await r.json()
            check("502 响应说明原因",
                  data.get("error") == "upstream_unreachable", str(data))
            r = await client.get("/auth/status")
            check("status 仍可用（本机信息不依赖云端）", r.status == 200, str(r.status))
        finally:
            await client.close()
            await mgr.aclose()

    anyio.run(body)


def run_all():
    PASS.clear(); FAIL.clear(); RESULTS.clear()
    for fn in (test_zero_regression, test_route_shapes, test_end_to_end,
               test_prefix_configurable, test_cloud_down):
        try:
            fn()
        except Exception as e:
            check(f"{fn.__name__} 整段异常", False, f"{type(e).__name__}: {e}")
    return {"results": list(RESULTS), "passed": len(PASS), "failed": len(FAIL),
            "failures": list(FAIL), "total": len(RESULTS)}


SABOTAGES = [
    ("无条件注册 /auth/*（不看 authm 是否为 None）",
     "破坏零回归：没配 endpoint 的用户也会多出 8 条路由",
     "server.py", lambda s: s.replace("    if authm is not None:\n        app[\"authm\"] = authm",
                                      "    if True:\n        app[\"authm\"] = authm")),
    ("云端不可达时返回 200",
     "把上游故障掩饰成成功，SPA 会以为发码成功",
     "server.py", lambda s: s.replace(
         "    if status == 0:\n        return _json(body, status=502)",
         "    if status == 0:\n        return _json(body, status=200)")),
    ("verify 不转发 deviceKey",
     "云端无法把会话绑到设备，R5 失效",
     "_auth_manager.py", lambda s: s.replace(
         'payload.update({"code": code, "deviceKey": self._device_key, "platform": self._platform})',
         'payload.update({"code": code})')),
    # 破坏点要打在真正处理非法 JSON 的那条路径上：request.json() 会抛异常，
    # 走的是 except 分支，而不是后面的 isinstance 判断。
    # 注意别把注释/noqa 写进匹配串——ruff 会改动它们，破坏点就静默失配了
    # （这正是它第一次被判「无效」的原因）。
    ("非法 JSON 被当成空对象",
     "前端拿不到「是 JSON 错了」这个信息，只能看到含糊的字段缺失",
     "server.py", lambda s: s.replace(
         "        body = await request.json()\n    except Exception:\n        return None",
         "        body = await request.json()\n    except Exception:\n        return {}")),
    ("status 里泄露 token",
     "SPA 拿到 token 即等于把凭证暴露给页面脚本",
     "_auth_manager.py", lambda s: s.replace(
         '            "loggedIn": bool(self._token),',
         '            "loggedIn": bool(self._token),\n            "token": self._token,')),
    ("云端路由前缀写死",
     "对着另一种部署形态（psi-cloud 根路径）会全部 404，且客户端发布后改不动",
     "_auth_manager.py", lambda s: s.replace(
         'url = f"{self.endpoint}{self.prefix}{path}"',
         'url = f"{self.endpoint}/api/auth{path}"')),
]

GATEWAY_DIR = os.path.join(os.path.abspath(FULL_SRC), "psi_agent", "gateway")


def run_negative():
    import contextlib
    import importlib
    import io
    import shutil

    print("反向验证：逐个破坏源码，确认路由自检能抓出来\n")
    all_caught = True
    for name, why, fname, mangle in SABOTAGES:
        path = os.path.join(GATEWAY_DIR, fname)
        orig = open(path, encoding="utf-8").read()
        mangled = mangle(orig)
        if mangled == orig:
            all_caught = False
            print(f"  [无效] {name}：破坏点未匹配到源码文本，需更新反向验证")
            continue
        backup = path + ".selfcheck-bak"
        shutil.copy2(path, backup)
        try:
            open(path, "w", encoding="utf-8").write(mangled)
            # 源码变了必须重新导入，否则测的还是内存里的旧模块
            for mod in list(sys.modules):
                if mod.startswith("psi_agent.gateway"):
                    del sys.modules[mod]
            importlib.invalidate_caches()
            globals().update(_reimport())
            with contextlib.redirect_stdout(io.StringIO()):
                s = run_all()
        finally:
            shutil.move(backup, path)
            for mod in list(sys.modules):
                if mod.startswith("psi_agent.gateway"):
                    del sys.modules[mod]
            importlib.invalidate_caches()
            globals().update(_reimport())
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


def _reimport():
    """重新导入被测符号（破坏源码后必须刷新）。"""
    from psi_agent.gateway._ai_manager import AIManager as A
    from psi_agent.gateway._auth_manager import AuthManager as Au
    from psi_agent.gateway._router_manager import RouterManager as R
    from psi_agent.gateway._session_manager import SessionManager as S
    from psi_agent.gateway._title_manager import TitleManager as T
    from psi_agent.gateway.server import create_app as C
    return {"AIManager": A, "AuthManager": Au, "RouterManager": R,
            "SessionManager": S, "TitleManager": T, "create_app": C}


def main():
    try:
        from pnvs_console import setup_console
        setup_console()
    except ImportError:
        pass
    if "--negative" in sys.argv:
        return run_negative()
    s = run_all()
    print(f"\n通过 {s['passed']} / {s['total']}，失败 {s['failed']}")
    if s["failed"]:
        print("失败项：" + "; ".join(s["failures"][:10]))
    else:
        print("零回归（实测）、八条路由、端到端转发、云端不可达处理均已验证。")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
