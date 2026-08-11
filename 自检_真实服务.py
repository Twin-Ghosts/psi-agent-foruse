# -*- coding: utf-8 -*-
"""用契约测试打真实服务（第 2 步验收的另一半）。

第 0 步的参考实现只证明契约可满足；这里证明**真实服务**满足同一份契约。
两者跑的是同一个 契约测试.py，所以不存在"参考实现绿、真服务另一套标准"。

    python 自检_真实服务.py
    python 自检_真实服务.py --negative
"""

import contextlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "contract"))

from app import service                      # noqa: E402
from app.server import build                 # noqa: E402
import 契约测试 as T                          # noqa: E402


def run_once():
    """起真实 aiohttp 服务，用同一份契约测试打它。

    契约测试是同步的（用 urllib），所以服务跑在后台线程的事件循环里 —— 这样
    既不用改契约测试，也能验真实 aiohttp 栈。契约测试传输无关，这正是它的价值。
    """
    import threading

    import anyio
    from aiohttp import web

    holder = {}
    ready = threading.Event()
    stop = threading.Event()

    async def serve_bg():
        app, svc = await build(test_hooks=True)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        holder["port"] = runner.addresses[0][1]
        ready.set()
        try:
            while not stop.is_set():
                await anyio.sleep(0.05)
        finally:
            await runner.cleanup()
            await svc.store.aclose()

    th = threading.Thread(target=lambda: anyio.run(serve_bg), daemon=True)
    th.start()
    if not ready.wait(timeout=30):
        raise RuntimeError("服务未在 30 秒内就绪")
    base = f"http://127.0.0.1:{holder['port']}"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            s = T.run_all(base)
        s["_log"] = buf.getvalue()
        return s
    finally:
        stop.set()
        th.join(timeout=10)


# 破坏点选"契约层面可观察"的：库内性质由 自检_服务层.py 负责。
SABOTAGES = [
    ("不做手机号归一化",
     lambda: _patch(service, "norm_phone",
                    lambda raw: (str(raw or "").strip() or None))),
    ("不做邮箱归一化",
     lambda: _patch(service, "norm_email",
                    lambda raw: (str(raw or "").strip() or None))),
    ("撤销设备不生效",
     lambda: _patch_method(service.AuthService, "revoke_device",
                           _revoke_noop)),
    ("验证码校验后不删除（可重放）",
     lambda: _patch_method(service.AuthService, "_check_email_code",
                           _check_keep)),
]


def _patch(mod, name, fn):
    orig = getattr(mod, name)
    setattr(mod, name, fn)
    return lambda: setattr(mod, name, orig)


def _patch_method(cls, name, fn):
    orig = getattr(cls, name)
    setattr(cls, name, fn)
    return lambda: setattr(cls, name, orig)


def _revoke_noop(self, token, device_id):
    sess = self.authenticate(token)
    dev = self.store.one("SELECT id FROM devices WHERE id=? AND user_id=?",
                         (device_id or "", sess["user_id"]))
    if not dev:
        raise service.ServiceError("not_found")
    return {"ok": True}


def _check_keep(self, identifier, code):
    import hmac as _hmac
    from app import providers as _p
    row = self.store.one(
        "SELECT code_hash, expires_at, attempts FROM email_codes"
        " WHERE identifier=?", (identifier,))
    if not row or row["expires_at"] < service.now_iso():
        raise service.ServiceError("code_expired")
    if _hmac.compare_digest(row["code_hash"],
                            _p.hash_code(identifier, str(code).strip())):
        return          # 不删：可重放
    raise service.ServiceError("invalid_code")


def main():
    try:
        from pnvs_console import setup_console
        setup_console()
    except ImportError:
        pass

    if "--negative" in sys.argv:
        print("反向验证：真实服务植入破坏点，确认契约测试能抓出来\n")
        all_caught = True
        for name, apply_fn in SABOTAGES:
            restore = apply_fn()
            try:
                s = run_once()
            finally:
                restore()
            caught = s["failed"] > 0
            all_caught = all_caught and caught
            print(f"  [{'抓到' if caught else '漏掉'}] {name}：失败 "
                  f"{s['failed']} 项"
                  + (f"，例如 {s['failures'][0]}" if caught else ""))
        healthy = run_once()
        print(f"\n  恢复后：失败 {healthy['failed']} 项（应为 0）")
        effective = all_caught and healthy["failed"] == 0
        print("\n  结论：" + ("每个破坏点都被抓到——契约测试对真实服务有约束力"
                              if effective else "有破坏点未被抓到，需修正"))
        return 0 if effective else 1

    s = run_once()
    print(f"真实服务跑契约测试：通过 {s['passed']} / {s['total']}，"
          f"失败 {s['failed']}")
    if s["failed"]:
        for r in s["results"]:
            if not r["ok"]:
                print(f"  FAIL {r['name']}  {r['detail'][:80]}")
    else:
        print("真实服务满足契约（与第 0 步参考实现跑的是同一份测试）。")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
