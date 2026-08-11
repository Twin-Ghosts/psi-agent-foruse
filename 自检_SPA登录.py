# -*- coding: utf-8 -*-
"""SPA v2 登录界面自检（第 8 步验收）。

三段，每段都是真跑，不是读代码：

1. **类型检查**：只统计我改动的文件。仓库里既有 17 个类型错误（renderBlobPreview
   与各 .test.ts，以及 tsconfig 的 TS7 兼容问题），那些不是本次引入的，也不在本次
   范围内修——所以这里按文件过滤，而不是要求全库零错误。
2. **构建**：`vite build` 必须成功。
3. **逻辑单测**：`authFlow.test.ts` 17 项，覆盖校验规则、错误码文案、倒计时回落、
   两段式判断。

反向验证：逐个破坏 authFlow.ts，确认单测转红。前端逻辑没有反向验证的话，
"17 项全过"跟"断言写成 expect(true).toBe(true)"没有区别。

**验不了的**：界面长什么样、交互顺不顺。我看不到渲染结果，那需要人眼看截图。

    python 自检_SPA登录.py
    python 自检_SPA登录.py --negative
"""

import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SPA = os.path.abspath(os.path.join(HERE, "..", "psi-agent-full", "src",
                                   "psi_agent", "gateway", "spa-v2"))
MY_FILES = ("HubLoginPanel", "services/api", "authFlow")

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


def run(cmd, timeout=420):
    """在 spa-v2 目录跑命令，返回 (退出码, 输出)。"""
    try:
        p = subprocess.run(cmd, cwd=SPA, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           shell=True)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"


def _temp_tsconfig():
    """去掉 TS7 已移除的 baseUrl/paths。

    刻意不改仓库里的 tsconfig.json：那两个错误是既有的（TypeScript 7 移除了
    baseUrl），修它属于另一件事，会污染本次 diff。
    """
    raw = open(os.path.join(SPA, "tsconfig.json"), encoding="utf-8").read()
    d = json.loads(re.sub(r"//.*", "", raw))
    d["compilerOptions"].pop("baseUrl", None)
    d["compilerOptions"].pop("paths", None)
    path = os.path.join(SPA, "tsconfig.selfcheck.json")
    open(path, "w", encoding="utf-8").write(json.dumps(d, indent=2))
    return path


def test_files_exist():
    section("[1] 改动落位")
    comp = os.path.join(SPA, "src", "components", "user-hub", "HubLoginPanel.tsx")
    flow = os.path.join(SPA, "src", "services", "authFlow.ts")
    test = os.path.join(SPA, "src", "services", "authFlow.test.ts")
    css = os.path.join(SPA, "src", "components", "user-hub", "user-hub.css")
    api = os.path.join(SPA, "src", "services", "api.ts")
    for label, path in (("HubLoginPanel.tsx", comp), ("authFlow.ts", flow),
                        ("authFlow.test.ts", test)):
        check(f"{label} 存在", os.path.isfile(path), path)

    src = open(comp, encoding="utf-8").read()
    check("占位文案已移除（原第 20 行「后续版本提供」）",
          "账号登录与云端同步将在后续版本提供" not in src,
          "占位还在，说明没真的替换")
    check("未配 endpoint 时仍保留本地模式说明",
          "本地 Gateway 模式" in src, "本地模式说明被删了")
    check("组件复用 authFlow 的逻辑而非自带一份",
          "from '../../services/authFlow'" in src)
    check("前端不写 localStorage/sessionStorage（token 只在 Gateway 侧）",
          "localStorage" not in src and "sessionStorage" not in src,
          "出现了浏览器存储，凭证可能被 XSS 读走")

    apisrc = open(api, encoding="utf-8").read()
    check("api.ts 里 /auth/status 对 404 回落为 available=false",
          "r.status === 404" in apisrc and "available: false" in apisrc)
    # 契约里 VerifyResult 确实有 token 字段（云端会返回，Gateway 侧消费），
    # 所以不能断言"类型里不出现 token"——那会误伤。真正该管的是：
    # 组件一行都不读它、不存它。前端持有凭证等于把 XSS 升级成凭证泄露。
    check("组件从不读取 token（只看 loggedIn 状态）",
          "token" not in src, "组件里出现了 token")
    check("status 类型里不含 token 字段",
          "credentialEncrypted" in apisrc
          and re.search(r"AuthStatus = \{[^}]*\btoken\b", apisrc,
                        re.S) is None,
          "AuthStatus 里出现了 token")

    csssrc = open(css, encoding="utf-8").read()
    for cls in (".hub-login-error", ".hub-spin"):
        check(f"CSS 定义了 {cls}（组件用到就必须有）", cls in csssrc)
    check("转圈动画尊重 prefers-reduced-motion",
          "prefers-reduced-motion" in csssrc)


def test_typecheck():
    section("[2] 类型检查（只看我改的文件）")
    cfg = _temp_tsconfig()
    try:
        code, out = run(["npx", "tsc", "--noEmit", "-p",
                         "tsconfig.selfcheck.json"])
    finally:
        try:
            os.unlink(cfg)
        except OSError:
            pass
    mine = [ln for ln in out.splitlines()
            if any(f in ln for f in MY_FILES) and "error" in ln]
    check("我改的文件零类型错误", not mine, "; ".join(mine[:3]))
    check("tsc 真的跑起来了（有输出或干净退出）",
          code in (0, 1, 2) and out is not None, f"exit={code}")


def test_build():
    section("[3] 构建")
    code, out = run(["npx", "vite", "build"])
    check("vite build 成功", code == 0 and "built in" in out,
          out.strip().splitlines()[-1] if out.strip() else f"exit={code}")
    dist = os.path.join(SPA, "dist", "index.html")
    check("产出 dist/index.html", os.path.isfile(dist), dist)


def test_unit():
    section("[4] 逻辑单测（vitest）")
    code, out = run(["npx", "vitest", "run", "src/services/authFlow.test.ts"])
    m = re.search(r"Tests\s+(\d+) passed", out)
    n = int(m.group(1)) if m else 0
    check("authFlow.test.ts 全部通过", code == 0 and n > 0,
          out.strip().splitlines()[-1] if out.strip() else f"exit={code}")
    check("单测数量不少于 15（少于此说明覆盖不足）", n >= 15, f"{n} 项")


def run_all(skip_build=False):
    PASS.clear(); FAIL.clear(); RESULTS.clear()
    fns = (test_files_exist, test_typecheck, test_unit) if skip_build else (
        test_files_exist, test_typecheck, test_build, test_unit)
    for fn in fns:
        try:
            fn()
        except Exception as e:
            check(f"{fn.__name__} 整段异常", False, f"{type(e).__name__}: {e}")
    return {"results": list(RESULTS), "passed": len(PASS), "failed": len(FAIL),
            "failures": list(FAIL), "total": len(RESULTS)}


FLOW = os.path.join(SPA, "src", "services", "authFlow.ts")
COMP = os.path.join(SPA, "src", "components", "user-hub", "HubLoginPanel.tsx")

# 破坏点作用于源码文本，逐个都必须让自检转红。
SABOTAGES = [
    ("手机号校验放宽（放过非大陆号段）",
     "前端放过、后端才拒，用户被绕一圈才知道号码不对",
     FLOW, lambda s: s.replace(r"const PHONE_RE = /^1[3-9]\d{9}$/",
                               r"const PHONE_RE = /^\d+$/")),
    ("倒计时缺失时回落成 0",
     "按钮立刻可再点，必然撞上服务端 60 秒限频",
     FLOW, lambda s: s.replace(
         "return typeof retryAfter === 'number' && retryAfter > 0 ? retryAfter : 60",
         "return typeof retryAfter === 'number' ? retryAfter : 0")),
    ("未知错误码统一显示「未知错误」",
     "吞掉线索，排查时看不出云端到底回了什么",
     FLOW, lambda s: s.replace(
         "return AUTH_ERROR_TEXT[raw] ?? raw",
         "return AUTH_ERROR_TEXT[raw] ?? '未知错误'")),
    ("删掉两个错误码文案",
     "用户会看到英文码",
     FLOW, lambda s: s.replace("  rate_limited: '操作过于频繁，请稍后再试',\n", "")
                      .replace("  provider_error: '短信/邮件服务暂时不可用，请稍后再试',\n", "")),
    ("有 token 时仍走 complete",
     "老用户登录会被当成新注册，重复建号",
     FLOW, lambda s: s.replace(
         "return Boolean(res.tempToken) && !res.token",
         "return Boolean(res.tempToken)")),
    ("前端把 token 存进 localStorage",
     "页面脚本持有凭证，XSS 即等于凭证泄露",
     COMP, lambda s: s.replace(
         "  const reset = () => {",
         "  localStorage.setItem('auth', 'x')\n\n  const reset = () => {")),
    ("删掉占位替换（把原占位文案放回去）",
     "等于第 8 步没做",
     COMP, lambda s: s.replace(
         "当前为<strong>本地 Gateway 模式</strong>",
         "账号登录与云端同步将在后续版本提供。当前为<strong>本地 Gateway 模式</strong>")),
]


def run_negative():
    import contextlib
    import io

    print("反向验证：逐个破坏源码，确认 SPA 自检能抓出来\n")
    all_caught = True
    for name, why, path, mangle in SABOTAGES:
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
            with contextlib.redirect_stdout(io.StringIO()):
                s = run_all()
        finally:
            shutil.move(backup, path)
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
    if not os.path.isdir(os.path.join(SPA, "node_modules")):
        print("需要先装依赖：cd spa-v2 && npm install（另需 vitest："
              "npm install --no-save vitest）")
        return 2
    if "--negative" in sys.argv:
        return run_negative()
    # --no-build 供日常冒烟用：跳过 vite build（最慢的一步）。
    # 合库前必须跑完整版——构建失败是真实交付事故，不能只靠类型检查兜。
    s = run_all(skip_build="--no-build" in sys.argv)
    print(f"\n通过 {s['passed']} / {s['total']}，失败 {s['failed']}")
    if s["failed"]:
        print("失败项：" + "; ".join(s["failures"][:10]))
    else:
        print("改动落位、类型检查、构建、逻辑单测均已验证。")
        print("未验证：界面外观与交互手感 —— 需人眼看截图，自检覆盖不到。")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
