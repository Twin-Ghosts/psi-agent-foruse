# -*- coding: utf-8 -*-
"""限频与配额自检（第 3 步验收）。

刻意排在接真供应商之前：限频错了，代价是真金白银和真实短信。mock 阶段验证零成本。

方案文档的两条顺序要求，这里各有专门断言：

    归一化必须在限频之前     否则 +86 / 86 / 带点号 Gmail 各占一个桶，等于绕过
    限频必须在调用供应商之前 供应商侧也有限频，但撞到它时钱已经花了

以及一条容易被忽略的：**校验路径也必须限频**。手机号验证码由 PNVS 托管，
但防爆破不会因此自动获得——6 位码只有 100 万种，不限校验次数即可穷举。

    python 自检_限频.py
    python 自检_限频.py --negative
"""

import os
import sys
import threading

import anyio
import time

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


async def new_svc():
    store = await Store(":memory:").open()
    return service.AuthService(store, providers.MockProvider())


async def send(svc, kind, ident, ip="9.9.9.9"):
    """返回 (是否成功, 错误码)。"""
    try:
        await svc.send_code(kind, ident, ip)
        return True, None
    except service.ServiceError as e:
        return False, e.code


async def test_order_vs_provider():
    section("[1] 限频必须发生在调用供应商之前（省钱的关键）")
    svc = await new_svc()
    ok1, _ = await send(svc, "email", "a@example.com")
    calls_after_first = svc.provider.calls
    check("首次发码调用了 provider", ok1 and calls_after_first == 1,
          f"ok={ok1} calls={calls_after_first}")

    ok2, code = await send(svc, "email", "a@example.com")
    check("同邮箱 60s 内重发被拒", not ok2 and code == "rate_limited", str(code))
    check("被限频时 provider 调用次数未增加",
          svc.provider.calls == calls_after_first,
          f"{calls_after_first} -> {svc.provider.calls}")

    # 同 IP 打满后同样不该碰供应商
    svc2 = await new_svc()
    for i in range(5):
        await send(svc2, "email", f"ip{i}@example.com", ip="7.7.7.7")
    n = svc2.provider.calls
    ok3, code3 = await send(svc2, "email", "ip99@example.com", ip="7.7.7.7")
    check("同 IP 打满后被拒", not ok3 and code3 == "rate_limited", str(code3))
    check("IP 限频命中时也不碰供应商", svc2.provider.calls == n,
          f"{n} -> {svc2.provider.calls}")


async def test_normalization_before_limit():
    section("[2] 归一化必须在限频之前")
    svc = await new_svc()
    ok1, _ = await send(svc, "phone", "13800000001")
    variants = ["+86 13800000001", "8613800000001", "138-0000-0001",
                " 13800000001 "]
    blocked = []
    for v in variants:
        ok, code = await send(svc, "phone", v)
        blocked.append(code == "rate_limited")
    check("首次发码成功", ok1)
    check("+86 / 86 / 横线 / 空格 四种写法都命中同一限频桶",
          all(blocked), str(list(zip(variants, blocked))))

    svc2 = await new_svc()
    await send(svc2, "email", "a.b.c@gmail.com")
    ok, code = await send(svc2, "email", "abc@gmail.com")
    check("Gmail 点号归一化后命中同一桶",
          code == "rate_limited", str(code))
    ok2, code2 = await send(svc2, "email", "a.b.c@example.com")
    check("非 Gmail 域名不去点号（不同人不该互相挤占）", ok2, str(code2))

    svc3 = await new_svc()
    await send(svc3, "email", "Case@Example.COM")
    ok3, code3 = await send(svc3, "email", "case@example.com")
    check("大小写归一化后命中同一桶", code3 == "rate_limited", str(code3))


async def test_verify_side_limit():
    section("[3] 校验路径必须限频（6 位码 100 万种，不限次即可穷举）")
    svc = await new_svc()
    await svc.send_code("email", "bf@example.com", "1.1.1.1")
    codes = [f"{i:06d}" for i in range(8)]
    outcomes = []
    for c in codes:
        try:
            await svc.verify("email", "bf@example.com", c, "dk", "win32")
            outcomes.append("ok")
        except service.ServiceError as e:
            outcomes.append(e.code)
    check("前几次错码返回 invalid_code 或 code_expired",
          outcomes[0] in ("invalid_code", "code_expired"), str(outcomes[:2]))
    check("连续错码最终触发 rate_limited",
          "rate_limited" in outcomes, str(outcomes))
    limit = service.RATE_LIMITS["verify_per_identifier"][1]
    check(f"限频在第 {limit + 1} 次前生效",
          outcomes.index("rate_limited") <= limit,
          f"第 {outcomes.index('rate_limited') + 1} 次才限频")

    # 手机号路径同样要限频（PNVS 托管≠自动获得防爆破）
    svc2 = await new_svc()
    await svc2.send_code("phone", "13900000002", "1.1.1.2")
    out2 = []
    for c in codes:
        try:
            await svc2.verify("phone", "13900000002", c, "dk", "win32")
            out2.append("ok")
        except service.ServiceError as e:
            out2.append(e.code)
    check("手机号校验路径也限频（托管不等于免疫爆破）",
          "rate_limited" in out2, str(out2))


async def test_isolation_and_window():
    section("[4] 桶隔离与窗口过期")
    svc = await new_svc()
    await send(svc, "email", "x1@example.com", ip="1.0.0.1")
    ok, code = await send(svc, "email", "x2@example.com", ip="1.0.0.2")
    check("不同邮箱、不同 IP 互不影响", ok, str(code))

    svc2 = await new_svc()
    for i in range(5):
        await send(svc2, "email", f"same{i}@example.com", ip="2.0.0.1")
    ok2, _ = await send(svc2, "email", "other@example.com", ip="2.0.0.2")
    check("一个 IP 打满不影响其它 IP", ok2)

    # 窗口过期后放行：把窗口起点改老，模拟时间流逝
    svc3 = await new_svc()
    await send(svc3, "email", "w@example.com", ip="3.0.0.1")
    ok3, code3 = await send(svc3, "email", "w@example.com", ip="3.0.0.1")
    check("窗口内被拒", code3 == "rate_limited")
    await svc3.store.write("UPDATE send_quota SET window_start=?",
                     (service.now_iso(-120),))
    ok4, code4 = await send(svc3, "email", "w@example.com", ip="3.0.0.1")
    check("窗口过期后放行", ok4, str(code4))

    # retryAfter 应为合理正数
    svc4 = await new_svc()
    await send(svc4, "email", "ra@example.com")
    try:
        await svc4.send_code("email", "ra@example.com", "9.9.9.9")
        check("被拒时带 retryAfter", False, "未抛错")
    except service.ServiceError as e:
        window = service.RATE_LIMITS["send_per_identifier"][0]
        check("被拒时带 retryAfter 且在窗口范围内",
              isinstance(e.retry_after, int)
              and 0 < e.retry_after <= window + 1, str(e.retry_after))


async def test_concurrent_limit():
    section("[5] 并发下限频不漏放")
    # 20 路并发抢同一邮箱的 1 次配额：只能有 1 个通过。
    # 若限频用"先查后写"且无事务保护，并发下会多放几个——那正是要抓的。
    svc = await new_svc()
    results = []

    # anyio 任务组而非线程：被测代码是 async，线程里 await 不了。
    async def worker():
        try:
            await svc.send_code("email", "conc@example.com", "4.0.0.1")
            results.append("ok")
        except service.ServiceError as e:
            results.append(e.code)
        except Exception as e:                            # noqa: BLE001
            results.append(f"other:{type(e).__name__}")

    async with anyio.create_task_group() as tg:
        for _ in range(20):
            tg.start_soon(worker)
    oks = results.count("ok")
    others = [r for r in results if r.startswith("other")]
    check("并发无非预期异常", not others, str(others[:3]))
    check("20 路并发只有 1 次通过（限频不漏放）", oks == 1,
          f"{oks} 次通过: {results[:6]}")
    check("provider 只被调用 1 次（漏放就是多花钱）",
          svc.provider.calls == 1, str(svc.provider.calls))


async def test_provider_failure():
    section("[6] 供应商失败不占配额")
    svc = await new_svc()
    svc.provider.fail_next = "HTTP 500 upstream boom"
    ok, code = await send(svc, "email", "pf@example.com")
    check("供应商失败时返回 provider_error", not ok and code == "provider_error",
          str(code))
    row = await svc.store.one("SELECT COUNT(*) c FROM email_codes"
                              " WHERE identifier=?", ("pf@example.com",))
    check("失败时不写入验证码记录（避免留一条用户永远收不到的码）",
          row["c"] == 0, "失败却写了记录")
    # 失败后立刻重试应放行：否则用户被我们自己的故障锁在门外
    ok2, code2 = await send(svc, "email", "pf@example.com")
    check("失败后可立即重试（identifier 配额已退还）", ok2, str(code2))
    row2 = await svc.store.one("SELECT COUNT(*) c FROM email_codes"
                               " WHERE identifier=?", ("pf@example.com",))
    check("重试成功后才有验证码记录", row2["c"] == 1)

    # IP 桶不退还：否则攻击者可借"制造失败"无限重试绕过 IP 限频
    svc2 = await new_svc()
    for i in range(4):
        await send(svc2, "email", f"q{i}@example.com", ip="8.8.8.8")
    svc2.provider.fail_next = "boom"
    await send(svc2, "email", "q9@example.com", ip="8.8.8.8")     # 第 5 次，失败
    ok3, code3 = await send(svc2, "email", "q10@example.com", ip="8.8.8.8")
    check("IP 配额不因供应商失败而退还（防借失败绕过 IP 限频）",
          not ok3 and code3 == "rate_limited", str(code3))


async def run_all():
    PASS.clear(); FAIL.clear(); RESULTS.clear()
    for fn in (test_order_vs_provider, test_normalization_before_limit,
               test_verify_side_limit, test_isolation_and_window,
               test_concurrent_limit, test_provider_failure):
        try:
            await fn()
        except Exception as e:
            check(f"{fn.__name__} 整段异常", False, f"{type(e).__name__}: {e}")
    return {"results": list(RESULTS), "passed": len(PASS), "failed": len(FAIL),
            "failures": list(FAIL), "total": len(RESULTS)}


SABOTAGES = [
    ("限频挪到调用供应商之后",
     "撞供应商的闸时钱已经花了",
     lambda: _patch_method(service.AuthService, "send_code", _send_late_limit)),
    ("不做手机号归一化",
     "同一个人可用 +86 / 86 等写法各占一个桶",
     lambda: _patch(service, "norm_phone",
                    lambda raw: (str(raw or "").strip() or None))),
    ("Gmail 不去点号",
     "a.b@gmail 与 ab@gmail 是同一人，却各占一个桶",
     lambda: _patch(service, "norm_email", _norm_email_nodots)),
    ("校验路径不限频",
     "6 位码 100 万种，不限次即可穷举",
     lambda: _patch_dict(service.RATE_LIMITS, "verify_per_identifier",
                         (300, 10**9))),
    ("同 IP 限频放大到无限",
     "一个 IP 就能把全站发码额度打满",
     lambda: _patch_dict(service.RATE_LIMITS, "send_per_ip", (60, 10**9))),
    ("限频改成先查后写（无事务保护）",
     "并发下会多放几个，等于多花钱",
     lambda: _patch_method(service.AuthService, "_hit", _hit_racy)),
    ("供应商失败后不退还配额",
     "用户会被我们自己的故障锁在门外 60 秒",
     lambda: _patch_method(service.AuthService, "_refund",
                           lambda self, scope, key: None)),
    ("供应商失败也退还 IP 配额",
     "攻击者可借制造失败无限重试，绕过 IP 限频",
     lambda: _patch_method(service.AuthService, "send_code",
                           _send_refund_ip)),
]


def _send_refund_ip(self, kind, raw_identifier, ip, invitation_code=None):
    """破坏点：失败时把 IP 配额也退了。"""
    identifier = (service.norm_phone if kind == "phone"
                  else service.norm_email)(raw_identifier)
    if not identifier:
        raise service.ServiceError("invalid_phone" if kind == "phone"
                                   else "invalid_email")
    window, limit = service.RATE_LIMITS["send_per_identifier"]
    ok, wait = self._hit("identifier", identifier, window, limit)
    if not ok:
        raise service.ServiceError("rate_limited", wait)
    window2, limit2 = service.RATE_LIMITS["send_per_ip"]
    ok, wait = self._hit("ip", ip, window2, limit2)
    if not ok:
        raise service.ServiceError("rate_limited", wait)
    code = providers.generate_code() if kind == "email" else None
    _id, err = self.provider.send_code(kind, identifier, code)
    if err:
        self._refund("identifier", identifier)
        self._refund("ip", ip)              # 破坏点在这
        raise service.ServiceError("provider_error")
    if kind == "email":
        self.store.write(
            "INSERT INTO email_codes(identifier, code_hash, expires_at,"
            " attempts, sent_at) VALUES (?,?,?,0,?)"
            " ON CONFLICT(identifier) DO UPDATE SET"
            " code_hash=excluded.code_hash, expires_at=excluded.expires_at,"
            " attempts=0, sent_at=excluded.sent_at",
            (identifier, providers.hash_code(identifier, code),
             service.now_iso(providers.CODE_TTL), service.now_iso()))
    return {"retryAfter": window}


def _patch(mod, name, fn):
    orig = getattr(mod, name)
    setattr(mod, name, fn)
    return lambda: setattr(mod, name, orig)


def _patch_method(cls, name, fn):
    orig = getattr(cls, name)
    setattr(cls, name, fn)
    return lambda: setattr(cls, name, orig)


def _patch_dict(d, key, value):
    orig = d[key]
    d[key] = value
    def restore():
        d[key] = orig
    return restore


def _norm_email_nodots(raw):
    s = str(raw or "").strip().lower()
    if s.count("@") != 1 or " " in s:
        return None
    local, _, domain = s.partition("@")
    if not local or "." not in domain:
        return None
    return f"{local}@{domain}"          # 不去点号


def _send_late_limit(self, kind, raw_identifier, ip, invitation_code=None):
    """破坏点：先调供应商再限频。"""
    identifier = (service.norm_phone if kind == "phone"
                  else service.norm_email)(raw_identifier)
    if not identifier:
        raise service.ServiceError("invalid_phone" if kind == "phone"
                                   else "invalid_email")
    code = providers.generate_code() if kind == "email" else None
    self.provider.send_code(kind, identifier, code)      # 钱已经花了
    window, limit = service.RATE_LIMITS["send_per_identifier"]
    ok, wait = self._hit("identifier", identifier, window, limit)
    if not ok:
        raise service.ServiceError("rate_limited", wait)
    if kind == "email":
        self.store.write(
            "INSERT INTO email_codes(identifier, code_hash, expires_at,"
            " attempts, sent_at) VALUES (?,?,?,0,?)"
            " ON CONFLICT(identifier) DO UPDATE SET"
            " code_hash=excluded.code_hash, expires_at=excluded.expires_at,"
            " attempts=0, sent_at=excluded.sent_at",
            (identifier, providers.hash_code(identifier, code),
             service.now_iso(providers.CODE_TTL), service.now_iso()))
    return {"retryAfter": window}


def _hit_racy(self, scope, key, window, limit):
    """破坏点：先查后写、查与写之间不持锁，并发下会多放。"""
    cutoff = service.now_iso(-window)
    rows = self.store.all("SELECT count FROM send_quota WHERE scope=? AND key=?"
                          " AND window_start >= ?", (scope, key, cutoff))
    total = sum(r["count"] for r in rows)
    if total >= limit:
        return False, 1
    time.sleep(0.002)                   # 放大竞态窗口
    self.store.write(
        "INSERT INTO send_quota(scope, key, window_start, count)"
        " VALUES (?,?,?,1) ON CONFLICT(scope, key, window_start)"
        " DO UPDATE SET count = count + 1", (scope, key, service.now_iso()))
    return True, 0


def run_negative():
    import contextlib
    import io
    print("反向验证：逐个植入破坏点，确认限频自检能抓出来\n")
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
        print("限频顺序、归一化前置、校验侧限频、桶隔离、并发、"
              "供应商失败处理均已验证（全程 mock，未花钱）。")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
