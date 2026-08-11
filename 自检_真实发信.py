# -*- coding: utf-8 -*-
"""自检：Resend 真实链路（会真发邮件，会消耗免费额度）。

与 `自检_供应商.py` 的分工：那个打本地假服务器，验的是"请求形状与解析对不
对"，不出公网；本文件验的是"真发得出去、真收得到、验证码真能校验通过"——
那是假服务器永远证明不了的一段。

收件人固定为 **Resend 账号自己的注册邮箱**：发信域名未完成 SPF+DKIM 验证时，
Resend 只允许发给账号本人（发给第三方域名返回 422 validation_error）。所以
这份自检不需要你手动去邮箱抄验证码 —— 它用 `GET /emails/{id}` 把刚发出的
正文取回来，从正文里解析验证码，再喂给真实的校验路径。

跑法：
    python 自检_真实发信.py                # 正向
    python 自检_真实发信.py --negative     # 反向验证（植入破坏点）

需要环境变量：
    AUTH_RESEND_API_KEY   Resend API key
    RESEND_SELFCHECK_TO   收件人（Resend 账号注册邮箱）
两者都会从同目录 `.env` 自动读取，不必手动 export。
"""

from __future__ import annotations

import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

import anyio  # noqa: E402
import httpx  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def load_env() -> None:
    """把同目录 .env 读进 os.environ（不覆盖已有值）。"""
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + ("" if ok else f"  {detail}"))


def section(title: str) -> None:
    print(f"\n{title}")


async def fetch_sent_email(api_key: str, mail_id: str, tries: int = 8) -> dict:
    """按 id 取回刚发出的邮件。刚发出时可能还查不到，短暂重试。"""
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        for i in range(tries):
            resp = await client.get(
                f"https://api.resend.com/emails/{mail_id}",
                headers={"Authorization": f"Bearer {api_key}"})
            if resp.status_code == 200:
                body = resp.json()
                if body.get("text") or body.get("html"):
                    return body
            await anyio.sleep(1.5 * (i + 1))
    return {}


async def run_all() -> dict:
    global PASSED, FAILED
    PASSED, FAILED = [], []

    from psi_cloud.auth import providers_core as providers
    from psi_cloud.auth import real_providers as R
    from psi_cloud.auth import service
    from psi_cloud.auth.store import Store

    api_key = os.environ.get("AUTH_RESEND_API_KEY", "").strip()
    to_addr = os.environ.get("RESEND_SELFCHECK_TO", "").strip()

    section("[0] 前置条件")
    check("已配置 AUTH_RESEND_API_KEY", bool(api_key), "缺 key，后续全部跳过")
    check("已配置 RESEND_SELFCHECK_TO", bool(to_addr), "缺收件人，后续全部跳过")
    if not api_key or not to_addr:
        return {"passed": len(PASSED), "failed": len(FAILED),
                "failures": list(FAILED)}

    section("[1] provider 按 AUTH_ 前缀读到凭据")
    prov = R.ResendProvider()
    # 这一条是移植时真实踩到的坑：代码只认裸名 RESEND_API_KEY，而 .env /
    # compose 注入的是 AUTH_RESEND_API_KEY，结果凭据配了却读不到、静默回落。
    check("ResendProvider 读到 AUTH_RESEND_API_KEY", prov.ready(),
          "ready() 为 False —— 环境变量名口径又对不上了")

    section("[2] 真实发信")
    code = providers.generate_code()
    mail_id, err = await prov.send_code("email", to_addr, code)
    check("发信成功并拿到邮件 id", bool(mail_id) and err is None, str(err))
    if not mail_id:
        return {"passed": len(PASSED), "failed": len(FAILED),
                "failures": list(FAILED)}

    section("[3] 收信侧核对（用 Resend API 取回正文，不必手抄）")
    got = await fetch_sent_email(api_key, mail_id)
    check("能取回刚发出的邮件", bool(got), "查不到该 id")
    body_text = (got.get("text") or "") + (got.get("html") or "")
    found = re.findall(r"\b(\d{6})\b", body_text)
    check("正文里含 6 位验证码", bool(found), f"正文片段：{body_text[:80]!r}")
    check("正文里的验证码与本地生成的一致", code in found,
          f"发的是 {code}，正文里是 {found}")
    check("投递状态不是失败",
          got.get("last_event") in ("delivered", "sent", "queued", "processed",
                                    "scheduled", None),
          f"last_event={got.get('last_event')}")

    section("[4] 真实验证码走完校验路径（发码→收信→校验→建号）")
    os.environ.setdefault("AUTH_CODE_HASH_SALT", "selfcheck-only-salt")
    store = await Store(":memory:").open()
    try:
        svc = service.AuthService(store, prov)
        # 走真实 send_code：它会自己生成验证码、真发信、把哈希写进 email_codes
        await svc.send_code("email", to_addr, "9.9.9.9")
        real_id = await store.one(
            "SELECT identifier, code_hash FROM email_codes WHERE identifier=?",
            (service.norm_email(to_addr),))
        check("email_codes 里落了一条哈希", bool(real_id),
              "发码没入库，后面校验必然过不了")
        check("库里存的不是验证码明文",
              bool(real_id) and len(real_id["code_hash"]) == 64,
              "code_hash 长度不像 sha256 摘要")

        # 从真实发出的那封邮件里取回验证码
        sent_id = None
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            resp = await client.get(
                "https://api.resend.com/emails?limit=5",
                headers={"Authorization": f"Bearer {api_key}"})
            if resp.status_code == 200:
                items = resp.json().get("data") or []
                if items:
                    sent_id = items[0].get("id")
        mail2 = await fetch_sent_email(api_key, sent_id) if sent_id else {}
        text2 = (mail2.get("text") or "") + (mail2.get("html") or "")
        codes2 = re.findall(r"\b(\d{6})\b", text2)
        check("取到第二封信里的验证码", bool(codes2), f"正文：{text2[:80]!r}")

        if codes2:
            # 必须捕获 ServiceError：破坏点植入后校验本就该失败，若让异常冒泡，
            # 反向验证会以"整段崩了"收场而不是记一条 FAIL —— 那等于没有断言。
            try:
                res = await svc.verify("email", to_addr, codes2[0],
                                       "dk-selfcheck", "windows")
            except service.ServiceError as exc:
                check("真实验证码校验通过", False, f"ServiceError: {exc.code}")
                check("命中即删：同一个码不能再用一次", False, "前一步就没通过")
            else:
                # 新用户走 tempToken 分支，老用户走 token 分支，两者都算校验通过
                ok = bool(res.get("tempToken") or res.get("token"))
                check("真实验证码校验通过", ok, str(res)[:120])
                check("命中即删：同一个码不能再用一次",
                      await code_reused_rejected(svc, to_addr, codes2[0]),
                      "同一个验证码被接受了两次 —— 防重放失效")
    finally:
        await store.aclose()

    return {"passed": len(PASSED), "failed": len(FAILED),
            "failures": list(FAILED)}


async def code_reused_rejected(svc, to_addr: str, code: str) -> bool:
    """同一个验证码再用一次必须被拒。"""
    from psi_cloud.auth import service
    try:
        await svc.verify("email", to_addr, code, "dk-selfcheck-2", "windows")
        return False
    except service.ServiceError:
        return True


SABOTAGES: list[tuple[str, str]] = [
    ("send_code 不真发就返回成功",
     "发信失败会被当成成功，用户等一封永远不到的邮件"),
    ("正文里不写验证码",
     "邮件发出去了但用户拿不到码，链路等于断的"),
    ("命中的验证码不删",
     "同一个码可重复使用，防重放失效"),
]


def run_negative() -> int:
    """逐个植入破坏点，确认本自检抓得住。

    破坏点都选**必然**改变行为的：曾经用过"只比摘要前两位"这种 1/256 才生效的
    破坏点，结果全绿、反向验证失效。
    """
    import contextlib
    import io

    from psi_cloud.auth import real_providers as R
    from psi_cloud.auth import service

    print("反向验证：逐个植入破坏点，确认真实链路自检能抓出来\n")
    all_caught = True

    for name, why in SABOTAGES:
        restore = apply_sabotage(name, R, service)
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
        time.sleep(65)   # 绕开自己的 60s/identifier 限频，别把失败归错因

    with contextlib.redirect_stdout(io.StringIO()):
        healthy = anyio.run(run_all)
    print(f"\n  恢复后：失败 {healthy['failed']} 项（应为 0）")
    effective = all_caught and healthy["failed"] == 0
    print("\n  结论：" + ("每个破坏点都被抓到，且恢复后全绿——自检有约束力"
                          if effective else "有破坏点未被抓到，需修正自检"))
    return 0 if effective else 1


def apply_sabotage(name: str, R, service):
    """植入一个破坏点，返回恢复函数。"""
    if name == "send_code 不真发就返回成功":
        orig = R.ResendProvider.send_code

        async def fake(self, provider, identifier, code):
            self.calls += 1
            return "fake-id-never-sent", None

        R.ResendProvider.send_code = fake
        return lambda: setattr(R.ResendProvider, "send_code", orig)

    if name == "正文里不写验证码":
        orig = R.ResendProvider.send_code

        async def no_code(self, provider, identifier, code):
            # 正文里放一串非 6 位数字的占位，邮件照样发出去，但用户拿不到码。
            # 破坏点必须**必然**改变行为：这里保证正文里不可能出现 6 位数字。
            return await orig(self, provider, identifier, "XXXXXX")

        R.ResendProvider.send_code = no_code
        return lambda: setattr(R.ResendProvider, "send_code", orig)

    if name == "命中的验证码不删":
        orig = service.AuthService.verify

        async def keep(self, kind, raw_identifier, code, device_key, platform):
            row = None
            if kind == "email":
                ident = service.norm_email(raw_identifier)
                row = await self.store.one(
                    "SELECT identifier, code_hash, expires_at, attempts,"
                    " sent_at FROM email_codes WHERE identifier=?", (ident,))
            res = await orig(self, kind, raw_identifier, code, device_key,
                             platform)
            if row is not None:      # 把刚被删掉的那条塞回去
                await self.store.write(
                    "INSERT OR REPLACE INTO email_codes(identifier, code_hash,"
                    " expires_at, attempts, sent_at) VALUES (?,?,?,?,?)",
                    (row["identifier"], row["code_hash"], row["expires_at"],
                     row["attempts"], row["sent_at"]))
            return res

        service.AuthService.verify = keep
        return lambda: setattr(service.AuthService, "verify", orig)

    raise AssertionError(f"未知破坏点：{name}")


def main() -> int:
    load_env()
    if "--negative" in sys.argv:
        return run_negative()

    print("自检：Resend 真实链路（会真发邮件）")
    print(f"收件人：{os.environ.get('RESEND_SELFCHECK_TO', '(未配置)')}")
    s = anyio.run(run_all)
    print(f"\n通过 {s['passed']} / {s['passed'] + s['failed']}，"
          f"失败 {s['failed']}")
    if s["failed"]:
        print("失败项：" + "; ".join(s["failures"]))
        return 1
    print("真实发信、收信核对、验证码校验、防重放均已验证（真发了邮件）。")
    print("尚未验证：自有域名发信（当前发件人是 onboarding@resend.dev，"
          "只能发往账号注册邮箱）——需完成 SPF+DKIM 验证。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
