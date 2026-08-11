# -*- coding: utf-8 -*-
"""跑 psi-agent 仓库风格的 pytest 测试（评审 Checklist 第五项「测试」）。

与 `自检_客户端认证.py` 的关系：那个是我自己的自检格式，覆盖面更广；这个是**按
psi-agent 仓库约定写的 pytest**，可直接随客户端改动一起进 `tests/`，评审时不必
额外解释测试怎么跑。两者都保留——前者验得细，后者能进对方仓库。

约定来自 psi-agent 的 pyproject：`testpaths = ["tests"]`、`asyncio_mode = "auto"`
（所以 async 测试不写装饰器）、`--strict-markers`。

    python 自检_pytest客户端.py
    python 自检_pytest客户端.py --negative
"""

import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FULL = os.path.abspath(os.path.join(HERE, "..", "psi-agent-full"))
GATEWAY = os.path.join(FULL, "src", "psi_agent", "gateway")

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


def run_pytest():
    """返回 (退出码, 输出, 通过数, 失败数)。"""
    env = dict(os.environ, PYTHONUTF8="1")
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/",
             "-p", "no:cacheprovider", "--no-cov", "-q"],
            cwd=FULL, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600, env=env)
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT", 0, 0
    out = (p.stdout or "") + (p.stderr or "")
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", out)) else 0
    return p.returncode, out, passed, failed


def test_suite_runs():
    section("[1] pytest 套件（psi-agent 仓库风格）")
    code, out, passed, failed = run_pytest()
    check("pytest 全部通过", code == 0 and failed == 0,
          out.strip().splitlines()[-1] if out.strip() else f"exit={code}")
    check("用例数不少于 15（少于此说明覆盖不足）", passed >= 15, f"{passed} 项")

    for rel in ("tests/conftest.py", "tests/test_auth_store.py",
                "tests/test_auth_manager.py"):
        check(f"{rel} 存在", os.path.isfile(os.path.join(FULL, rel)))

    # 约定符合性：asyncio_mode=auto 意味着不该出现 @pytest.mark.asyncio
    for rel in ("tests/test_auth_store.py", "tests/test_auth_manager.py"):
        src = open(os.path.join(FULL, rel), encoding="utf-8").read()
        check(f"{os.path.basename(rel)} 未写多余的 asyncio 装饰器"
              "（pyproject 已设 asyncio_mode=auto）",
              "@pytest.mark.asyncio" not in src)

    # 不该引入仓库没声明的插件依赖
    mgr_src = open(os.path.join(FULL, "tests", "test_auth_manager.py"),
                   encoding="utf-8").read()
    # 只看代码行，不看注释：注释里"刻意不用 aiohttp_server"这句会误伤
    code_lines = [ln for ln in mgr_src.splitlines()
                  if not ln.lstrip().startswith("#")]
    used_as_fixture = any(
        "aiohttp_server" in ln and ("def " in ln or "await aiohttp_server" in ln)
        for ln in code_lines)
    check("不依赖 pytest-aiohttp（仓库 dev 依赖里没有它）",
          not used_as_fixture,
          "把 aiohttp_server 当 fixture 用了，别人机器上跑不起来")


# 破坏点：每个都必须让 pytest 转红。断言若只是"能跑起来"，
# 那和 assert True 没区别。
SABOTAGES = [
    ("token 明文落盘", "_auth_store.py", lambda s: s.replace(
        'data["token"] = base64.b64encode(_xor(token.encode("utf-8"), key)).decode()',
        'data["token"] = token')),
    ("登出连 device_key 一起清", "_auth_store.py", lambda s: s.replace(
        '        data.pop("token", None)\n        data.pop("enc", None)',
        '        data = {}')),
    ("去掉解密校验和", "_auth_store.py", lambda s: s.replace(
        "        if want and _checksum(plain) != want:", "        if False:")),
    ("401 不清本机凭证", "_auth_manager.py", lambda s: s.replace(
        "        if status == _UNAUTHORIZED and self._token:",
        "        if False and self._token:")),
    ("verify 不带 deviceKey", "_auth_manager.py", lambda s: s.replace(
        'payload.update({"code": code, "deviceKey": self._device_key,'
        ' "platform": self._platform})',
        'payload.update({"code": code})')),
    ("登出时云端不可达就不清本机", "_auth_manager.py", lambda s: s.replace(
        "        await self.logout_local()\n        if status == 0:",
        "        if status == 0:")),
]


def run_negative():
    """逐个破坏源码，确认 pytest 转红。

    **恢复必须万无一失。** 早先版本用 ``shutil.move(bak, path)`` 恢复，某次
    move 抛了 FileNotFoundError，结果被破坏的源码留在了工作区里（`logout()`
    少了一行清凭证）。这类事故比测试本身失败严重得多：它会静静地改坏产品代码。
    现在改为：内存里留一份原文，用 write 覆盖恢复（不依赖临时文件是否还在），
    恢复后立刻比对内容，不一致就大声报错并停止后续破坏。
    """
    print("反向验证：逐个破坏源码，确认 pytest 转红\n")
    all_caught = True
    for name, fname, mangle in SABOTAGES:
        path = os.path.join(GATEWAY, fname)
        orig = open(path, encoding="utf-8").read()
        mangled = mangle(orig)
        if mangled == orig:
            all_caught = False
            print(f"  [无效] {name}：破坏点未匹配到源码文本，需更新反向验证")
            continue
        try:
            open(path, "w", encoding="utf-8").write(mangled)
            code, out, passed, failed = run_pytest()
        finally:
            # 用内存里的原文覆盖回去，并核对确实恢复了
            open(path, "w", encoding="utf-8").write(orig)
            restore_ok = open(path, encoding="utf-8").read() == orig
        if not restore_ok:
            # 不在 finally 里 return：那会吞掉异常，真正的错误就看不见了
            print(f"\n  严重：{fname} 恢复失败，源码可能仍是被破坏状态！"
                  f"\n  请立即检查 {path}")
            return 1
        caught = failed > 0
        all_caught = all_caught and caught
        print(f"  [{'抓到' if caught else '漏掉'}] {name}"
              + (f"：{failed} 项失败" if caught else "：pytest 仍全绿"))
    code, out, passed, failed = run_pytest()
    print(f"\n  恢复后：{passed} 通过 / {failed} 失败（失败应为 0）")
    effective = all_caught and failed == 0
    print("\n  结论：" + ("每个破坏点都被抓到，且恢复后全绿——测试有约束力"
                          if effective else "有破坏点未被抓到，需修正测试"))
    return 0 if effective else 1


def run_all():
    PASS.clear(); FAIL.clear(); RESULTS.clear()
    try:
        test_suite_runs()
    except Exception as e:
        check("test_suite_runs 整段异常", False, f"{type(e).__name__}: {e}")
    return {"results": list(RESULTS), "passed": len(PASS), "failed": len(FAIL),
            "failures": list(FAIL), "total": len(RESULTS)}


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
        print("pytest 套件可跑、覆盖足够、符合仓库约定、无额外插件依赖。")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
