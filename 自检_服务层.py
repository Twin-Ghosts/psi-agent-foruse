# -*- coding: utf-8 -*-
"""服务层自检：契约测不到的那些性质（第 2 步验收的一半）。

为什么需要这个文件：契约测试只看 HTTP 行为，看不见库里存了什么。把 token 明文
存进 sessions 表，契约测试 60/60 依旧全绿——这是反向验证当场暴露出来的缺口。
凡是"从外部观察不到、但错了会出事"的性质，都必须在这一层断言：

    token / 验证码只存哈希，库里搜不到原文
    撤销是标记 revoked_at，不是删行（要留审计痕迹）
    last_used_at 每次请求都刷新（滑动过期的前提）
    重装同一 device_key 不刷出新设备行
    并发建号只落一个 user

    python 自检_服务层.py
    python 自检_服务层.py --negative
"""

import os
import sys
import threading

import anyio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import providers, service          # noqa: E402
from app.store import Store                 # noqa: E402

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


async def new_svc(**kw):
    store = await Store(":memory:").open()
    return service.AuthService(store, providers.MockProvider(),
                              **kw)


async def register(svc, email="a@example.com", device_key="dk-1"):
    """走完整流程拿到 token。"""
    await svc.send_code("email", email, "1.1.1.1")
    code = svc.provider.peek_code(service.norm_email(email))
    r = await svc.verify("email", email, code, device_key, "win32")
    if "token" in r:
        return r["token"], code
    r2 = await svc.complete(r["tempToken"], device_key, "win32")
    return r2["token"], code


async def test_no_plaintext():
    section("[1] 库里不得留明文（契约测不到，只能在这一层验）")
    svc = await new_svc()
    token, code = await register(svc)

    # 把整库倒成文本再搜：比逐列检查更难漏
    dump = await svc.store.dump()
    check("sessions 表搜不到 token 原文", token not in dump,
          "token 明文出现在库里")
    check("email_codes 表搜不到验证码原文", code not in dump,
          "验证码明文出现在库里")

    row = await svc.store.one("SELECT token_hash FROM sessions")
    check("token_hash 是 SHA256 十六进制（64 位）",
          row and len(row["token_hash"]) == 64, str(dict(row) if row else None))
    check("token_hash 与 token 不同", row["token_hash"] != token)
    check("同一 token 摘要稳定（否则查不到会话）",
          row["token_hash"] == service.token_hash(token))

    # 验证码摘要同理
    await svc.send_code("email", "b@example.com", "1.1.1.2")
    c2 = svc.provider.peek_code("b@example.com")
    r = await svc.store.one("SELECT code_hash FROM email_codes WHERE identifier=?",
                      ("b@example.com",))
    check("code_hash 是 SHA256 十六进制", r and len(r["code_hash"]) == 64)
    check("code_hash 里不含验证码原文", c2 not in r["code_hash"])


async def test_session_semantics():
    section("[2] 会话语义")
    svc = await new_svc()
    token, _ = await register(svc)

    before = (await svc.store.one("SELECT last_used_at, id FROM sessions"))["last_used_at"]
    import time as _t
    _t.sleep(1.1)                   # ISO 秒级精度，必须跨过一秒
    await svc.me(token)
    after = (await svc.store.one("SELECT last_used_at FROM sessions"))["last_used_at"]
    check("每次请求刷新 last_used_at（滑动过期的前提）", after > before,
          f"{before} -> {after}")

    n_before = (await svc.store.one("SELECT COUNT(*) c FROM sessions"))["c"]
    await svc.logout(token)
    n_after = (await svc.store.one("SELECT COUNT(*) c FROM sessions"))["c"]
    row = await svc.store.one("SELECT revoked_at FROM sessions")
    check("登出是标记 revoked_at 而非删行（留审计痕迹）",
          n_after == n_before and row["revoked_at"] is not None,
          f"{n_before} -> {n_after}, revoked_at={row['revoked_at']}")

    try:
        await svc.me(token)
        check("登出后 token 立即失效", False, "仍能通过鉴权")
    except service.ServiceError as e:
        check("登出后 token 立即失效", e.code == "unauthorized", e.code)

    # 过期会话不可用
    svc2 = await new_svc()
    t2, _ = await register(svc2, "exp@example.com")
    await svc2.store.write("UPDATE sessions SET expires_at=?",
                     (service.now_iso(-10),))
    try:
        await svc2.me(t2)
        check("过期 token 被拒", False, "过期仍可用")
    except service.ServiceError as e:
        check("过期 token 被拒", e.code == "unauthorized", e.code)


async def test_device_identity():
    section("[3] 设备与身份")
    svc = await new_svc()
    await register(svc, "d@example.com", "same-key")
    await svc.reset_limits()
    # 同一 device_key 再登录：不该新增设备行（重装不刷出新设备）
    await svc.send_code("email", "d@example.com", "1.1.1.3")
    c = svc.provider.peek_code("d@example.com")
    await svc.verify("email", "d@example.com", c, "same-key", "win32")
    n = (await svc.store.one("SELECT COUNT(*) c FROM devices"))["c"]
    check("同 device_key 复登录不新增设备行", n == 1, f"{n} 行")

    await svc.reset_limits()
    await svc.send_code("email", "d@example.com", "1.1.1.4")
    c = svc.provider.peek_code("d@example.com")
    await svc.verify("email", "d@example.com", c, "other-key", "darwin")
    n2 = (await svc.store.one("SELECT COUNT(*) c FROM devices"))["c"]
    check("不同 device_key 新增设备行", n2 == 2, f"{n2} 行")

    users = (await svc.store.one("SELECT COUNT(*) c FROM users"))["c"]
    check("三次登录只有一个 user（登录不重复建号）", users == 1, f"{users} 个")


async def test_concurrency():
    section("[4] 并发建号只落一个 user")
    svc = await new_svc()
    await svc.send_code("email", "race@example.com", "1.1.1.5")
    code = svc.provider.peek_code("race@example.com")
    r = await svc.verify("email", "race@example.com", code, "dk-r", "win32")
    tt = r.get("tempToken")

    results = []

    # 用 anyio 任务组而非线程 + Barrier：被测代码现在是 async，线程里 await 不了。
    # 任务组里 8 个任务并发跑，同样能撞出竞态（改 async 后确实撞出过一个：
    # 验证码的"读一次+删一次"之间会插进其它请求）。
    async def worker(i):
        try:
            out = await svc.complete(tt, f"dk-{i}", "win32")
            results.append(("ok", out.get("token")))
        except service.ServiceError as e:
            results.append(("err", e.code))
        except Exception as e:                            # noqa: BLE001
            results.append(("other", repr(e)[:60]))

    async with anyio.create_task_group() as tg:
        for i in range(8):
            tg.start_soon(worker, i)

    oks = [r for r in results if r[0] == "ok"]
    others = [r for r in results if r[0] == "other"]
    users = (await svc.store.one("SELECT COUNT(*) c FROM users"))["c"]
    idents = (await svc.store.one("SELECT COUNT(*) c FROM identities"))["c"]
    check("并发无非预期异常", not others, str(others[:2]))
    check("tempToken 只能用一次（8 路并发只有 1 个成功）",
          len(oks) == 1, f"{len(oks)} 个成功: {results}")
    check("库里只有一个 user", users == 1, f"{users} 个")
    check("库里只有一条 identity", idents == 1, f"{idents} 条")


async def test_revoke():
    section("[5] 设备撤销（R5）")
    svc = await new_svc()
    t1, _ = await register(svc, "rv@example.com", "dk-a")
    await svc.reset_limits()
    await svc.send_code("email", "rv@example.com", "1.1.1.6")
    c = svc.provider.peek_code("rv@example.com")
    t2 = (await svc.verify("email", "rv@example.com", c, "dk-b", "darwin"))["token"]

    devs = (await svc.list_devices(t1))["devices"]
    check("设备列表含两台", len(devs) == 2, str(len(devs)))
    check("恰有一台标 current",
          sum(1 for d in devs if d["current"]) == 1, str(devs))

    target = next(d for d in devs if not d["current"])
    await svc.revoke_device(t1, target["id"])
    try:
        await svc.me(t2)
        check("被踢设备立即 401（不用 JWT 的核心理由）", False, "仍可用")
    except service.ServiceError as e:
        check("被踢设备立即 401（不用 JWT 的核心理由）",
              e.code == "unauthorized", e.code)
    check("当前设备不受影响", (await svc.me(t1))["user"]["id"] is not None)
    check("撤销后设备列表只剩一台",
          len((await svc.list_devices(t1))["devices"]) == 1)

    try:
        await svc.revoke_device(t1, "no-such")
        check("撤销不存在的设备报 not_found", False, "未报错")
    except service.ServiceError as e:
        check("撤销不存在的设备报 not_found", e.code == "not_found", e.code)

    # 不能踢别人的设备
    svc2_token, _ = await register(svc, "other@example.com", "dk-x")
    other_dev = (await svc.list_devices(svc2_token))["devices"][0]["id"]
    try:
        await svc.revoke_device(t1, other_dev)
        check("不能撤销他人设备", False, "越权成功了")
    except service.ServiceError as e:
        check("不能撤销他人设备", e.code == "not_found", e.code)


async def run_all():
    PASS.clear(); FAIL.clear(); RESULTS.clear()
    for fn in (test_no_plaintext, test_session_semantics, test_device_identity,
               test_concurrency, test_revoke):
        try:
            await fn()
        except Exception as e:
            check(f"{fn.__name__} 整段异常", False, f"{type(e).__name__}: {e}")
    return {"results": list(RESULTS), "passed": len(PASS), "failed": len(FAIL),
            "failures": list(FAIL), "total": len(RESULTS)}


# 破坏点必须逐个被抓到。第一个尤其重要：它正是契约测试漏掉的那条
# （token 明文入库时契约 60/60 依旧全绿），本文件存在的理由。
SABOTAGES = [
    ("token 明文入库（契约测不到）",
     "库被读走即等于所有 token 泄露",
     lambda: _patch(service, "token_hash", lambda t: t)),
    ("验证码明文入库",
     "同理，库被读走即可冒充任何人完成注册",
     lambda: _patch(providers, "hash_code",
                    lambda ident, code: f"{ident}:{code}")),
    ("登出改成删行",
     "丢失审计痕迹，也无法区分'从未登录'与'已登出'",
     lambda: _patch_method(
         service.AuthService, "logout",
         lambda self, token: (
             self.store.write("DELETE FROM sessions WHERE token_hash=?",
                              (service.token_hash(token),)),
             {"ok": True})[1])),
    ("不刷新 last_used_at",
     "滑动过期失效，活跃用户会被强制登出",
     lambda: _patch_method(
         service.AuthService, "authenticate",
         lambda self, token: _auth_no_touch(self, token))),
    ("撤销设备不写 revoked_at",
     "R5 的即时撤销失效",
     lambda: _patch_method(
         service.AuthService, "revoke_device",
         lambda self, token, device_id: _revoke_noop(self, token, device_id))),
    ("device_key 每次都建新设备行",
     "重装即刷出新设备，设备列表失真",
     lambda: _patch_method(
         service.AuthService, "_issue_session",
         lambda self, uid, dk, plat: _issue_always_new(self, uid, dk, plat))),
]


def _patch(mod, name, fn):
    orig = getattr(mod, name)
    setattr(mod, name, fn)
    return lambda: setattr(mod, name, orig)


def _patch_method(cls, name, fn):
    orig = getattr(cls, name)
    setattr(cls, name, fn)
    return lambda: setattr(cls, name, orig)


def _auth_no_touch(self, token):
    if not token:
        raise service.ServiceError("unauthorized")
    row = self.store.one(
        "SELECT * FROM sessions WHERE token_hash=? AND revoked_at IS NULL",
        (service.token_hash(token),))
    if not row or row["expires_at"] < service.now_iso():
        raise service.ServiceError("unauthorized")
    return row


def _revoke_noop(self, token, device_id):
    sess = self.authenticate(token)
    dev = self.store.one("SELECT id FROM devices WHERE id=? AND user_id=?",
                         (device_id or "", sess["user_id"]))
    if not dev:
        raise service.ServiceError("not_found")
    return {"ok": True}


def _issue_always_new(self, uid, device_key, platform):
    import uuid as _u
    import secrets as _s
    with self.store.tx() as conn:
        did = f"d_{_u.uuid4().hex[:16]}"
        conn.execute("INSERT INTO devices(id, user_id, device_key, platform,"
                     " created_at) VALUES (?,?,?,?,?)",
                     (did, uid, device_key, platform, service.now_iso()))
        token = _s.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions(id, user_id, device_id, token_hash,"
            " created_at, last_used_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            (f"s_{_u.uuid4().hex[:16]}", uid, did, service.token_hash(token),
             service.now_iso(), service.now_iso(),
             service.now_iso(service.TOKEN_TTL_DAYS * 86400)))
    return token


def run_negative():
    import contextlib
    import io
    print("反向验证：逐个植入破坏点，确认服务层自检能抓出来\n")
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
        print("库里无明文、会话语义、设备身份、并发建号、设备撤销均已验证。")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
