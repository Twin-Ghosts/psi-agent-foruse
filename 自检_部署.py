# -*- coding: utf-8 -*-
"""部署配置自检（第 4 步验收，本地可验的部分）。

**能在这里验的**：配置文件语法与关键条目、备份逻辑（纯 SQLite，可真跑）、
测试钩子在生产入口默认关闭、健康检查不依赖供应商。

**只能在你的服务器上验的**（本文件不假装验证）：
    HTTPS 可访问、证书自动签发   需要公网 DNS 指向与 80/443
    容器重启不丢库               需要 Docker
    Caddy 转发真实客户端 IP      需要真实反代链路
这三条的验收命令写在 deploy/验收清单.md 里。

    python 自检_部署.py
    python 自检_部署.py --negative
"""

import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading

import anyio

HERE = os.path.dirname(os.path.abspath(__file__))
DEPLOY = os.path.join(HERE, "deploy")
sys.path.insert(0, HERE)

from app import service  # noqa: E402

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


def read(rel):
    p = os.path.join(DEPLOY, rel)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""



class _Serving:
    """在后台线程的事件循环里起 aiohttp 服务，给同步自检用。

    自检本身是同步的（用 urllib），服务是 async 的；用线程隔开比把整个自检
    改成 async 更省事，也不影响被测对象。
    """

    def __init__(self, **kw):
        self.kw = kw
        self.base = ""
        self.svc = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._th = None

    def __enter__(self):
        holder = {}
        # host 是本类自己的参数，不能透传给 build()
        host = self.kw.pop("host", "127.0.0.1")

        async def go():
            from aiohttp import web

            from app.server import build
            app, svc = await build(**self.kw)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host, 0)
            await site.start()
            holder["port"] = runner.addresses[0][1]
            holder["svc"] = svc
            self._ready.set()
            try:
                while not self._stop.is_set():
                    await anyio.sleep(0.05)
            finally:
                await runner.cleanup()
                await svc.store.aclose()

        self._th = threading.Thread(target=lambda: anyio.run(go), daemon=True)
        self._th.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("服务未就绪")
        self.base = f"http://127.0.0.1:{holder['port']}"
        self.svc = holder["svc"]
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._th.join(timeout=10)
        return False


def test_caddy():
    section("[1] Caddyfile 关键条目")
    c = read("Caddyfile")
    check("Caddyfile 存在", bool(c))
    check("转发真实客户端 IP（否则所有用户共用一个限频桶）",
          "X-Forwarded-For" in c and "{remote_host}" in c)
    check("测试钩子被挡在公网之外（它会回显验证码）",
          "/__test__/*" in c and "404" in c)
    for h in ("Strict-Transport-Security", "X-Content-Type-Options",
              "X-Frame-Options", "Referrer-Policy"):
        check(f"安全头 {h}", h in c)
    check("不回显 Server 版本", "-Server" in c)
    check("安装包分发挂在同一 Caddy 上", "handle_path /downloads/*" in c)
    check("反代指向 auth 服务", re.search(r"reverse_proxy\s+auth:8000", c))
    # 大括号配对：Caddyfile 语法错会导致容器起不来
    check("大括号配对", c.count("{") == c.count("}"),
          f"{{ {c.count('{')} 个, }} {c.count('}')} 个")


def test_compose():
    section("[2] docker-compose.yml")
    y = read("docker-compose.yml")
    check("compose 文件存在", bool(y))
    check("SQLite 数据挂 volume（留容器层则重建即丢库）",
          "auth-data:/data" in y)
    check("声明了 volumes", re.search(r"^volumes:", y, re.M))
    # 只看 auth 那一段：整文件正则会跨段匹配到 caddy 的 ports
    auth_block = y.split("caddy:")[0]
    check("auth 不对宿主映射端口（只允许经 Caddy 访问）",
          "expose:" in auth_block and "ports:" not in auth_block,
          "auth 段出现了 ports")
    check("caddy 映射 80/443", '"80:80"' in y and '"443:443"' in y)
    check("auth 配了 healthcheck", "healthcheck:" in y)
    check("caddy 等 auth 健康后再起",
          "condition: service_healthy" in y)
    check("重启策略 unless-stopped", y.count("restart: unless-stopped") >= 2)
    # 凭据必须来自环境变量且缺失即报错（:?required）
    for k in ("ALIYUN_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_SECRET",
              "RESEND_API_KEY", "EMAIL_CODE_SALT"):
        check(f"{k} 由环境注入且缺失即拒绝启动",
              f"${{{k}:?required}}" in y)
    check("compose 里不含任何明文凭据",
          not re.search(r"(re_[A-Za-z0-9]{12,}|LTAI[A-Za-z0-9]{12,})", y))
    check("备份服务以只读挂载源库", "auth-data:/data:ro" in y)

    try:
        import yaml
        yaml.safe_load(y)
        check("YAML 可解析", True)
    except ImportError:
        check("YAML 可解析（未装 pyyaml，跳过深度校验）", True)
    except Exception as e:
        check("YAML 可解析", False, str(e)[:80])


def test_dockerfile():
    section("[3] Dockerfile")
    d = ""
    p = os.path.join(HERE, "Dockerfile")
    if os.path.exists(p):
        d = open(p, encoding="utf-8").read()
    check("Dockerfile 存在", bool(d))
    check("以非 root 运行", "USER appuser" in d)
    # 只看 CMD/ENTRYPOINT 行，不能整文件搜——注释里提到这个词是正常的
    cmd_lines = [ln for ln in d.splitlines()
                 if ln.strip().startswith(("CMD", "ENTRYPOINT"))]
    check("生产启动命令不带 --test-hooks",
          cmd_lines and all("--test-hooks" not in ln for ln in cmd_lines),
          str(cmd_lines))
    check("设置了 PYTHONUTF8（中文日志不乱码）", "PYTHONUTF8" in d)


def test_backup_script():
    section("[4] 备份脚本内容")
    b = read("backup.sh")
    check("backup.sh 存在", bool(b))
    # 正向要求"存在真实的 VACUUM INTO 调用"，而不是"没出现 cp"——
    # 后者是个弱断言：把 VACUUM INTO 删掉、也没写 cp 的脚本会照样通过。
    vacuum_lines = [ln for ln in b.splitlines()
                    if "VACUUM INTO" in ln and not ln.strip().startswith(
                        ("#", "--", "rem"))]
    check("确实调用了 VACUUM INTO（非注释行）", bool(vacuum_lines),
          "找不到有效的 VACUUM INTO 调用")
    check("不用 cp 备份主库",
          not re.search(r"^\s*cp\s+.*\$DB", b, re.M))
    check("备份后校验 integrity_check", "integrity_check" in b)
    check("校验失败则报错退出", "备份完整性校验失败" in b)
    check("以只读方式打开源库", "mode=ro" in b)
    check("清理旧备份（否则磁盘迟早写满）", "BACKUP_KEEP" in b)


def test_backup_really_works():
    section("[5] 备份逻辑真跑一遍（纯 SQLite，本地可验）")
    anyio.run(_backup_body)


async def _backup_body():
    from app import providers, service
    from app.store import Store

    tmp = tempfile.mkdtemp()
    dbp = os.path.join(tmp, "auth.db")
    # 造一个有真实数据的库
    store = await Store(dbp).open()
    svc = service.AuthService(store, providers.MockProvider())
    await svc.send_code("email", "bk@example.com", "1.1.1.1")
    code = svc.provider.peek_code("bk@example.com")
    r = await svc.verify("email", "bk@example.com", code, "dk", "win32")
    await svc.complete(r["tempToken"], "dk", "win32")
    users_before = (await svc.store.one("SELECT COUNT(*) c FROM users"))["c"]
    check("源库有数据", users_before == 1, str(users_before))

    # VACUUM INTO 出一份备份
    out = os.path.join(tmp, "backup.db")
    src = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    try:
        src.execute("VACUUM INTO ?", (out,))
    finally:
        src.close()
    check("VACUUM INTO 产出备份文件", os.path.exists(out))

    chk = sqlite3.connect(out)
    try:
        integrity = chk.execute("PRAGMA integrity_check").fetchone()[0]
        tables = chk.execute("SELECT COUNT(*) FROM sqlite_master"
                             " WHERE type='table'").fetchone()[0]
        users = chk.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        idents = chk.execute("SELECT COUNT(*) FROM identities").fetchone()[0]
    finally:
        chk.close()
    check("备份 integrity_check = ok", integrity == "ok", str(integrity))
    check("备份含全部 7 张表", tables == 7, str(tables))
    check("备份的数据可读且一致", users == users_before and idents == 1,
          f"users={users} idents={idents}")

    # 备份可作为源库直接使用（恢复演练）
    rstore = await Store(out).open()
    restored = service.AuthService(rstore, providers.MockProvider())
    n = (await restored.store.one("SELECT COUNT(*) c FROM users"))["c"]
    check("备份可直接当库用（恢复演练通过）", n == 1, str(n))
    await restored.store.aclose()

    # 关键对照：WAL 下直接 cp 主库文件，可能丢掉尚在 WAL 里的事务。
    # 裸 cp 出来的文件甚至可能缺表（连 schema 都没落盘）——那正是不能用 cp 的理由。
    await svc.send_code("email", "after@example.com", "1.1.1.2")
    live = (await svc.store.one("SELECT COUNT(*) c FROM email_codes"))["c"]
    plain = os.path.join(tmp, "plain-cp.db")
    with open(dbp, "rb") as f_in, open(plain, "wb") as f_out:
        f_out.write(f_in.read())        # 模拟 cp，不带 -wal / -shm
    c2 = sqlite3.connect(plain)
    try:
        try:
            got = c2.execute("SELECT COUNT(*) FROM email_codes").fetchone()[0]
            missing_table = False
        except sqlite3.OperationalError:
            got, missing_table = 0, True    # 连表都没有，更说明问题
    finally:
        c2.close()
    check("对照：裸 cp 拿不到完整数据（故必须用 VACUUM INTO）",
          live == 1 and (missing_table or got < live),
          f"live={live} cp={got} 缺表={missing_table}")

    await svc.store.aclose()


def test_container_reachability():
    section("[7] 容器内可达性与并发（本地自检曾漏掉这三条）")
    # 这三条是 23/23 全绿时仍然存在的真 bug，补进来防复发。
    import socket as _socket
    import threading as _th
    from app.server import serve

    # 1. 绑定地址：只绑 127.0.0.1 时，Caddy（另一个容器）连不上 -> 必然 502
    y = read("docker-compose.yml")
    check("compose 显式传 AUTH_BIND", "AUTH_BIND" in y)
    check("默认绑 0.0.0.0（容器内可达）",
          "AUTH_BIND:-0.0.0.0" in y, "默认值不是 0.0.0.0")
    srv, base, _svc = serve(host="0.0.0.0", sweep_interval=0)
    try:
        port = srv.server_address[1]
        # 用本机非回环地址连一次：绑 127.0.0.1 时这一步会失败
        s = _socket.socket()
        s.settimeout(3)
        try:
            s.connect((_socket.gethostbyname(_socket.gethostname()), port))
            reachable = True
        except OSError:
            reachable = False
        finally:
            s.close()
        check("绑 0.0.0.0 后可从非回环地址连上（Caddy 走的就是这条路）",
              reachable, "非回环地址连不上，容器里 Caddy 会 502")
    finally:
        srv.shutdown()

    # 2. 并发：单线程 HTTPServer 会把请求串行化
    srv, base, svc = serve(test_hooks=True, sweep_interval=0)
    try:
        import json as _json
        import urllib.request as _u
        results = []
        lock = _th.Lock()

        def hit(i):
            try:
                req = _u.Request(f"{base}/api/auth/otp",
                                 _json.dumps({"email": f"c{i}@example.com"}).encode(),
                                 {"Content-Type": "application/json",
                                  "X-Forwarded-For": f"10.9.{i}.1"})
                with _u.urlopen(req, timeout=10) as r:
                    with lock:
                        results.append(r.status)
            except Exception as e:                        # noqa: BLE001
                with lock:
                    results.append(repr(e)[:40])

        ts = [_th.Thread(target=hit, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("6 路并发请求全部拿到响应（单线程会超时或排队）",
              results.count(200) == 6, str(results))
    finally:
        srv.shutdown()

    # 3. 定时清理：必须有周期性调用者，不能只有测试钩子
    src = open(os.path.join(HERE, "app", "server.py"), encoding="utf-8").read()
    check("存在周期性 sweep 的调用者（不只是测试钩子）",
          "_sweep_loop" in src and "sweep_interval" in src,
          "生产环境没有任何东西会定期清过期数据")
    # 上一条只查符号在不在，挡不住"把启动条件改成 if False"这种绕过。
    # 必须确认那个线程真的会被启动。
    check("清理线程的启动条件未被短路",
          re.search(r"if\s+sweep_interval\s*>\s*0\s*:", src) is not None,
          "启动条件被改写，清理线程实际不会跑")
    check("生产入口传入了非零清理间隔",
          re.search(r"AUTH_SWEEP_INTERVAL[^\n]*3600", src) is not None,
          "生产默认没开清理")
    check("compose 配了清理间隔", "AUTH_SWEEP_INTERVAL" in y)
    # 真跑一轮：间隔设 1 秒，插一条过期数据，看它是否被清掉
    srv, base, svc = serve(test_hooks=True, sweep_interval=1)
    try:
        svc.store.write(
            "INSERT INTO email_codes(identifier, code_hash, expires_at, sent_at)"
            " VALUES ('stale@x.com','h',?,?)",
            (service.now_iso(-100), service.now_iso(-400)))
        before = svc.store.one("SELECT COUNT(*) c FROM email_codes")["c"]
        import time as _t
        _t.sleep(2.5)
        after = svc.store.one("SELECT COUNT(*) c FROM email_codes")["c"]
        check("定时清理真的跑了（过期行被自动删除）",
              before >= 1 and after == 0, f"{before} -> {after}")
    finally:
        srv.shutdown()


def test_prod_defaults():
    section("[6] 生产默认值安全")
    import json as _json
    import urllib.error
    import urllib.request

    with _Serving() as s:
        try:
            urllib.request.urlopen(f"{s.base}/__test__/provider_calls", timeout=5)
            check("默认不暴露测试钩子", False, "钩子可访问")
        except urllib.error.HTTPError as e:
            check("默认不暴露测试钩子", e.code == 404, str(e.code))
        with urllib.request.urlopen(f"{s.base}/healthz", timeout=5) as r:
            body = _json.loads(r.read())
        check("/healthz 返回 ok", body.get("ok") is True, str(body))
        check("/healthz 不调用供应商（否则供应商抖动会反复重启容器）",
              s.svc.provider.calls == 0, str(s.svc.provider.calls))
        check("邀请码门禁默认关闭", s.svc.invitation_required is False)


def test_container_reachability():
    section("[7] 容器内可达性与并发（本地自检曾漏掉这三条）")
    import json as _json
    import socket as _socket
    import urllib.request

    y = read("docker-compose.yml")
    check("compose 显式传 AUTH_BIND", "AUTH_BIND" in y)
    check("默认绑 0.0.0.0（容器内可达）",
          "AUTH_BIND:-0.0.0.0" in y, "默认值不是 0.0.0.0")

    # 1. 绑 0.0.0.0 后必须能从非回环地址连上（Caddy 走的就是这条路）
    with _Serving(host="0.0.0.0") as s:
        port = int(s.base.rsplit(":", 1)[1])
        sock = _socket.socket()
        sock.settimeout(3)
        try:
            sock.connect((_socket.gethostbyname(_socket.gethostname()), port))
            reachable = True
        except OSError:
            reachable = False
        finally:
            sock.close()
        check("绑 0.0.0.0 后可从非回环地址连上", reachable,
              "非回环地址连不上，容器里 Caddy 会 502")

    # 2. 并发：aiohttp 单进程事件循环也要能同时处理多个请求
    with _Serving(test_hooks=True) as s:
        results = []
        lock = threading.Lock()

        def hit(i):
            try:
                req = urllib.request.Request(
                    f"{s.base}/api/auth/otp",
                    _json.dumps({"email": f"c{i}@example.com"}).encode(),
                    {"Content-Type": "application/json",
                     "X-Forwarded-For": f"10.9.{i}.1"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    with lock:
                        results.append(r.status)
            except Exception as e:                        # noqa: BLE001
                with lock:
                    results.append(repr(e)[:40])

        ts = [threading.Thread(target=hit, args=(i,)) for i in range(6)]
        [th.start() for th in ts]
        [th.join() for th in ts]
        check("6 路并发请求全部拿到响应", results.count(200) == 6, str(results))

    # 3. 定时清理必须有周期性调用者，不能只有测试钩子
    src = open(os.path.join(HERE, "app", "server.py"), encoding="utf-8").read()
    check("存在周期性 sweep 的调用者（不只是测试钩子）",
          "_sweep_loop" in src and "sweep_interval" in src,
          "生产环境没有任何东西会定期清过期数据")
    check("清理线程的启动条件未被短路",
          re.search(r"if\s+sweep_interval\s*>\s*0\s*:", src) is not None,
          "启动条件被改写，清理线程实际不会跑")
    check("生产入口传入了非零清理间隔",
          re.search(r"AUTH_SWEEP_INTERVAL.*3600", src) is not None,
          "生产默认没开清理")
    check("compose 配了清理间隔", "AUTH_SWEEP_INTERVAL" in y)

    # 真跑一轮：间隔 1 秒，插一条过期数据，看它是否被自动清掉
    anyio.run(_sweep_really_runs)


async def _sweep_really_runs():
    from app import service as _svc_mod
    from app.server import build
    app, svc = await build(test_hooks=True)
    try:
        await svc.store.write(
            "INSERT INTO email_codes(identifier, code_hash, expires_at, sent_at)"
            " VALUES ('stale@x.com','h',?,?)",
            (_svc_mod.now_iso(-100), _svc_mod.now_iso(-400)))
        before = (await svc.store.one("SELECT COUNT(*) c FROM email_codes"))["c"]
        async with anyio.create_task_group() as tg:
            from app.server import _sweep_loop
            tg.start_soon(_sweep_loop, svc, 1)
            await anyio.sleep(2.5)
            tg.cancel_scope.cancel()
        after = (await svc.store.one("SELECT COUNT(*) c FROM email_codes"))["c"]
        check("定时清理真的跑了（过期行被自动删除）",
              before >= 1 and after == 0, f"{before} -> {after}")
    finally:
        await svc.store.aclose()


def run_all():
    PASS.clear(); FAIL.clear(); RESULTS.clear()
    for fn in (test_caddy, test_compose, test_dockerfile, test_backup_script,
               test_backup_really_works, test_prod_defaults,
               test_container_reachability):
        try:
            fn()
        except Exception as e:
            check(f"{fn.__name__} 整段异常", False, f"{type(e).__name__}: {e}")
    return {"results": list(RESULTS), "passed": len(PASS), "failed": len(FAIL),
            "failures": list(FAIL), "total": len(RESULTS)}


# 破坏点作用于配置文件文本或代码，逐个都必须被抓到
SABOTAGES = [
    ("Caddy 不转发真实客户端 IP",
     "所有用户共用一个限频桶，send_per_ip 形同虚设",
     "Caddyfile", lambda s: s.replace("X-Forwarded-For", "X-Nothing")),
    ("测试钩子暴露到公网",
     "钩子会回显验证码，等于任何人可登录任何账号",
     "Caddyfile", lambda s: s.replace("handle /__test__/* {", "handle /nope/* {")),
    ("去掉 HSTS 安全头",
     "允许降级到 HTTP，token 可能被明文传输",
     "Caddyfile", lambda s: s.replace("Strict-Transport-Security", "X-Whatever")),
    ("数据不挂 volume",
     "重建容器即丢库",
     "docker-compose.yml", lambda s: s.replace("auth-data:/data",
                                               "./tmpdata:/data")),
    ("凭据改成有默认值",
     "缺失凭据时静默启动，跑到线上才发现发不出信",
     "docker-compose.yml",
     lambda s: s.replace("${RESEND_API_KEY:?required}", "${RESEND_API_KEY:-}")),
    ("Caddy 不等 auth 健康就起",
     "启动瞬间的请求会 502",
     "docker-compose.yml",
     lambda s: s.replace("condition: service_healthy",
                         "condition: service_started")),
    ("备份改用 cp",
     "WAL 下 cp 可能拿到不一致甚至损坏的库",
     "backup.sh", lambda s: s.replace('\tpython - "$DB" "$out" <<\'PY\'',
                                      '\tcp "$DB" "$out"\n\tfalse <<\'PY\'')),
    ("删掉 VACUUM INTO 且不做任何备份",
     "脚本看似正常，实际什么都没备份",
     "backup.sh", lambda s: s.replace("VACUUM INTO ?", "SELECT 1 -- ?")),
    # 以下三条对应 23/23 全绿时仍存在的真 bug，加破坏点防复发
    ("绑定地址退回 127.0.0.1",
     "容器里 Caddy 是另一个容器，只绑回环必然 502",
     "docker-compose.yml", lambda s: s.replace(
         "AUTH_BIND: ${AUTH_BIND:-0.0.0.0}",
         "AUTH_BIND: ${AUTH_BIND:-127.0.0.1}")),
    ("去掉定时清理",
     "过期数据无人清，慢慢积垢且不报错",
     "../app/server.py", lambda s: s.replace(
         "    if sweep_interval > 0:", "    if False:  # sweep disabled")),
    ("备份不校验完整性",
     "备份文件存在不等于可用，等真要恢复时才发现是坏的",
     "backup.sh", lambda s: s.replace("integrity_check", "quick_look")),
]


def run_negative():
    import contextlib
    import io
    import shutil
    print("反向验证：逐个破坏配置，确认部署自检能抓出来\n")
    all_caught = True
    for name, why, fname, mangle in SABOTAGES:
        path = os.path.join(DEPLOY, fname)
        orig = open(path, encoding="utf-8").read()
        mangled = mangle(orig)
        if mangled == orig:
            all_caught = False
            print(f"  [无效] {name}：破坏点未匹配到文本，需更新反向验证")
            continue
        backup = path + ".bak"
        shutil.copy2(path, backup)
        try:
            open(path, "w", encoding="utf-8").write(mangled)
            with contextlib.redirect_stdout(io.StringIO()):
                s = run_all()
        finally:
            # 用内存里的原文覆盖恢复，不依赖临时文件是否还在。
            # 早先用 shutil.move 恢复过一次失败，把被破坏的源码留在了
            # 工作区里——这类事故会静静改坏产品代码，比测试失败严重。
            open(path, "w", encoding="utf-8").write(orig)
            restore_ok = open(path, encoding="utf-8").read() == orig
        if not restore_ok:
            # 不在 finally 里 return: 那会吞掉异常, 真正的错误就看不见了
            print("  严重: 恢复失败, 请立即检查 " + str(path))
            return 1
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
    if "--negative" in sys.argv:
        return run_negative()
    s = run_all()
    print(f"\n通过 {s['passed']} / {s['total']}，失败 {s['failed']}")
    if s["failed"]:
        print("失败项：" + "; ".join(s["failures"][:10]))
    else:
        print("配置条目、备份逻辑（真跑）、生产默认值均已验证。")
        print("注意：HTTPS 可访问 / 证书自动签发 / 重启不丢库 / 真实 IP 透传"
              "这四条只能在服务器上验，见 deploy/验收清单.md。")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
