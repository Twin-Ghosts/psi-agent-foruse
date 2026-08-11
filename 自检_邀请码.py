# -*- coding: utf-8 -*-
"""邀请码门禁自检（第 9 步验收）。

排在最后：它是独立开关且默认关闭，不阻塞任何前置步骤。

验四件事：
    默认关闭时行为完全不变（不能因为加了门禁把正常注册弄坏）
    开启后无码 / 错码 / 过期码 / 已用码 一律被拒
    并发消费同一邀请码只有一个成功（乐观锁）
    绑定邮箱的邀请码不能被他人使用

    python 自检_邀请码.py
    python 自检_邀请码.py --negative
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


async def new_svc(required=False):
    store = await Store(":memory:").open()
    return service.AuthService(store, providers.MockProvider(),
                               invitation_required=required)


async def add_code(svc, code, expires_at=None, bound=None, status="unused"):
    await svc.store.write(
        "INSERT INTO invitation_codes(code, status, expires_at,"
        " bound_identifier, created_at) VALUES (?,?,?,?,?)",
        (code, status, expires_at, bound, service.now_iso()))


async def register(svc, email, invitation=None, device_key="dk"):
    """走完整注册流程，返回 (是否成功, 错误码)。"""
    try:
        await svc.send_code("email", email, "1.1.1.1", invitation)
        code = svc.provider.peek_code(service.norm_email(email))
        r = await svc.verify("email", email, code, device_key, "win32")
        if "token" in r:
            return True, None
        await svc.complete(r["tempToken"], device_key, "win32",
                     invitation_code=invitation)
        return True, None
    except service.ServiceError as e:
        return False, e.code


async def test_default_off():
    section("[1] 默认关闭时行为不变")
    svc = await new_svc()
    check("invitation_required 默认为 False", svc.invitation_required is False)
    ok, code = await register(svc, "off@example.com")
    check("不给邀请码也能注册", ok, str(code))
    ok2, code2 = await register(svc, "off2@example.com", invitation="WHATEVER")
    check("给了不存在的邀请码也不影响（门禁关闭时忽略）", ok2, str(code2))
    users = (await svc.store.one("SELECT COUNT(*) c FROM users"))["c"]
    check("两个账号都建成了", users == 2, str(users))


async def test_gate_on():
    section("[2] 开启后的拦截")
    svc = await new_svc(required=True)
    ok, code = await register(svc, "no@example.com")
    check("无邀请码被拒", not ok and code == "invitation_required", str(code))
    ok, code = await register(svc, "bad@example.com", invitation="NOPE")
    check("不存在的邀请码被拒", not ok and code == "invitation_invalid",
          str(code))
    check("被拒时未建账号",
          (await svc.store.one("SELECT COUNT(*) c FROM users"))["c"] == 0)

    await add_code(svc, "GOOD-1")
    ok, code = await register(svc, "good@example.com", invitation="GOOD-1")
    check("有效邀请码可注册", ok, str(code))
    row = await svc.store.one("SELECT status, consumed_at FROM invitation_codes"
                        " WHERE code='GOOD-1'")
    check("注册后邀请码标为 consumed", row["status"] == "consumed",
          str(dict(row)))
    check("记录了消费时间", row["consumed_at"] is not None)

    # 已用过的不能再用
    await svc.reset_limits()
    ok, code = await register(svc, "reuse@example.com", invitation="GOOD-1",
                        device_key="dk2")
    check("已消费的邀请码不能重复使用",
          not ok and code == "invitation_invalid", str(code))

    # 过期
    await add_code(svc, "EXPIRED-1", expires_at=service.now_iso(-60))
    await svc.reset_limits()
    ok, code = await register(svc, "exp@example.com", invitation="EXPIRED-1")
    check("过期邀请码被拒", not ok and code == "invitation_invalid", str(code))

    # 已撤销
    await add_code(svc, "REVOKED-1", status="revoked")
    await svc.reset_limits()
    ok, code = await register(svc, "rev@example.com", invitation="REVOKED-1")
    check("已撤销邀请码被拒", not ok and code == "invitation_invalid", str(code))


async def test_bound_code():
    section("[3] 绑定邮箱的邀请码")
    svc = await new_svc(required=True)
    await add_code(svc, "BOUND-1", bound="owner@example.com")
    ok, code = await register(svc, "someone@example.com", invitation="BOUND-1")
    check("他人不能使用已绑定的邀请码",
          not ok and code == "invitation_invalid", str(code))
    await svc.reset_limits()
    ok2, code2 = await register(svc, "owner@example.com", invitation="BOUND-1")
    check("绑定者本人可以使用", ok2, str(code2))


async def test_concurrent_consume():
    section("[4] 并发消费只有一个成功（乐观锁）")
    svc = await new_svc(required=True)
    await add_code(svc, "RACE-1")

    # 先各自拿到 tempToken（发码与校验都要过门禁的非消费性检查）
    temps = []
    for i in range(6):
        await svc.reset_limits()
        email = f"race{i}@example.com"
        await svc.send_code("email", email, "1.1.1.1", "RACE-1")
        c = svc.provider.peek_code(email)
        r = await svc.verify("email", email, c, f"dk{i}", "win32")
        temps.append(r["tempToken"])

    results = []

    # anyio 任务组而非线程：被测代码是 async，线程里 await 不了。
    async def worker(i, tt):
        try:
            await svc.complete(tt, f"dk{i}", "win32", invitation_code="RACE-1")
            results.append("ok")
        except service.ServiceError as e:
            results.append(e.code)
        except Exception as e:                            # noqa: BLE001
            results.append(f"other:{type(e).__name__}")

    async with anyio.create_task_group() as tg:
        for i, tt in enumerate(temps):
            tg.start_soon(worker, i, tt)

    others = [r for r in results if r.startswith("other")]
    check("并发无非预期异常", not others, str(others[:2]))
    check("6 路并发只有 1 个消费成功", results.count("ok") == 1,
          f"{results.count('ok')} 个成功: {results}")
    row = await svc.store.one("SELECT status FROM invitation_codes"
                        " WHERE code='RACE-1'")
    check("邀请码状态为 consumed", row["status"] == "consumed", str(dict(row)))
    users = (await svc.store.one("SELECT COUNT(*) c FROM users"))["c"]
    check("只建出一个账号", users == 1, f"{users} 个")


async def run_all():
    PASS.clear(); FAIL.clear(); RESULTS.clear()
    for fn in (test_default_off, test_gate_on, test_bound_code,
               test_concurrent_consume):
        try:
            await fn()
        except Exception as e:
            check(f"{fn.__name__} 整段异常", False, f"{type(e).__name__}: {e}")
    return {"results": list(RESULTS), "passed": len(PASS), "failed": len(FAIL),
            "failures": list(FAIL), "total": len(RESULTS)}


SABOTAGES = [
    ("门禁默认改成开启",
     "会把现有用户的正常注册全部挡掉",
     lambda: _patch_default(True)),
    ("不校验邀请码状态",
     "已消费的码可无限复用",
     lambda: _patch_method(service.AuthService, "_check_invitation",
                           _check_ignore_status)),
    ("消费不用乐观锁（先查后写）",
     "并发下同一个码会被消费多次",
     lambda: _patch_method(service.AuthService, "_check_invitation",
                           _check_racy)),
    ("不校验过期时间",
     "过期邀请码仍可用",
     lambda: _patch_method(service.AuthService, "_check_invitation",
                           _check_no_expiry)),
    ("不校验绑定邮箱",
     "定向发放的邀请码可被他人抢用",
     lambda: _patch_method(service.AuthService, "_check_invitation",
                           _check_no_bound)),
]


def _patch_default(value):
    orig = service.AuthService.__init__

    def patched(self, store, provider=None, invitation_required=False):
        orig(self, store, provider, value)
    service.AuthService.__init__ = patched
    return lambda: setattr(service.AuthService, "__init__", orig)


def _patch_method(cls, name, fn):
    orig = getattr(cls, name)
    setattr(cls, name, fn)
    return lambda: setattr(cls, name, orig)


def _base_row(self, code):
    if not code:
        raise service.ServiceError("invitation_required")
    row = self.store.one(
        "SELECT code, status, expires_at, bound_identifier"
        " FROM invitation_codes WHERE code=?", (code,))
    if not row:
        raise service.ServiceError("invitation_invalid")
    return row


def _check_ignore_status(self, code, identifier, consume):
    _base_row(self, code)           # 不看 status


def _check_racy(self, code, identifier, consume):
    import time as _t
    row = _base_row(self, code)
    if row["status"] != "unused":
        raise service.ServiceError("invitation_invalid")
    if consume:
        _t.sleep(0.003)             # 放大竞态窗口
        self.store.write("UPDATE invitation_codes SET status='consumed',"
                         " consumed_at=? WHERE code=?",
                         (service.now_iso(), code))   # 无 status 条件


def _check_no_expiry(self, code, identifier, consume):
    row = _base_row(self, code)
    if row["status"] != "unused":
        raise service.ServiceError("invitation_invalid")
    if consume:
        self.store.write("UPDATE invitation_codes SET status='consumed',"
                         " consumed_at=? WHERE code=? AND status='unused'",
                         (service.now_iso(), code))


def _check_no_bound(self, code, identifier, consume):
    row = _base_row(self, code)
    if row["status"] != "unused":
        raise service.ServiceError("invitation_invalid")
    if row["expires_at"] and row["expires_at"] < service.now_iso():
        raise service.ServiceError("invitation_invalid")
    if consume:
        self.store.write("UPDATE invitation_codes SET status='consumed',"
                         " consumed_at=? WHERE code=? AND status='unused'",
                         (service.now_iso(), code))


def run_negative():
    import contextlib
    import io
    print("反向验证：逐个植入破坏点，确认邀请码自检能抓出来\n")
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
        print("默认关闭时零影响、开启后各类拦截、绑定校验、并发乐观锁均已验证。")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
