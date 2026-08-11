# -*- coding: utf-8 -*-
"""跑齐当前所有验收。CI 与本地都用这一个入口。

    python verify.py             完整验收（正向 + 反向），约 4 分钟
    python verify.py --quick     日常冒烟：只跑正向，约 40 秒

**--quick 通过不等于验收通过。** 它跳过反向验证，因此只能回答"当前实现没退化"，
回答不了"这些断言是否真的在约束什么"。合库前、交付前必须跑完整套件。

原则：每一步的验收都必须包含反向验证。只跑正向、全绿不构成证据——必须确认
"故意弄坏会变红"。这条不是洁癖：本项目已有三次"看似全绿其实无效"的实例
（概率性破坏点、破坏 SQL 语法而非约束、破坏点匹配注释被格式化工具改掉）。
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# (标签, 相对路径, 参数)
SUITES = [
    ("第 0 步 契约：对未实现的空服务（应全红→报 exit 1，此处期望失败）",
     "contract/契约测试.py", ["--unimplemented"], "expect_fail"),
    ("第 0 步 契约：对参考实现（应全绿）",
     "contract/契约测试.py", ["--reference"], "expect_pass"),
    ("第 0 步 契约：反向验证（每个破坏点都要被抓到）",
     "contract/契约测试.py", ["--negative"], "expect_pass"),
    ("第 1 步 schema：约束与清理（应全绿）",
     "自检_schema.py", [], "expect_pass"),
    ("第 1 步 schema：反向验证（每个破坏点都要被抓到）",
     "自检_schema.py", ["--negative"], "expect_pass"),
    ("第 2 步 服务层：库里无明文 / 会话 / 设备 / 并发（应全绿）",
     "自检_服务层.py", [], "expect_pass"),
    ("第 2 步 服务层：反向验证（每个破坏点都要被抓到）",
     "自检_服务层.py", ["--negative"], "expect_pass"),
    ("第 2 步 契约：真实服务跑契约测试（应全绿）",
     "自检_真实服务.py", [], "expect_pass"),
    ("第 2 步 契约：真实服务反向验证",
     "自检_真实服务.py", ["--negative"], "expect_pass"),
    ("第 3 步 限频：顺序 / 归一化 / 校验侧 / 并发（应全绿）",
     "自检_限频.py", [], "expect_pass"),
    ("第 3 步 限频：反向验证（每个破坏点都要被抓到）",
     "自检_限频.py", ["--negative"], "expect_pass"),
    ("第 9 步 邀请码：默认关闭零影响 / 拦截 / 并发（应全绿）",
     "自检_邀请码.py", [], "expect_pass"),
    ("第 9 步 邀请码：反向验证（每个破坏点都要被抓到）",
     "自检_邀请码.py", ["--negative"], "expect_pass"),
    ("第 4 步 部署：配置 / 备份真跑 / 生产默认值（应全绿）",
     "自检_部署.py", [], "expect_pass"),
    ("第 4 步 部署：反向验证（每个破坏点都要被抓到）",
     "自检_部署.py", ["--negative"], "expect_pass"),
    ("第 5/6 步 供应商：签名 / 两层判据 / 分发（应全绿）",
     "自检_供应商.py", [], "expect_pass"),
    ("第 5/6 步 供应商：反向验证（每个破坏点都要被抓到）",
     "自检_供应商.py", ["--negative"], "expect_pass"),
    ("第 7 步 客户端：凭证加密 / device_key / 401 清凭证（应全绿）",
     "自检_客户端认证.py", [], "expect_pass"),
    ("第 7 步 客户端：反向验证（每个破坏点都要被抓到）",
     "自检_客户端认证.py", ["--negative"], "expect_pass"),
    ("第 7 步 路由：零回归实测 / 8 条路由 / 端到端（应全绿）",
     "自检_路由注册.py", [], "expect_pass"),
    ("第 7 步 路由：反向验证（每个破坏点都要被抓到）",
     "自检_路由注册.py", ["--negative"], "expect_pass"),
    ("第 7 步 pytest：psi-agent 仓库风格测试（应全绿）",
     "自检_pytest客户端.py", [], "expect_pass"),
    ("第 7 步 pytest：反向验证（每个破坏点都要被抓到）",
     "自检_pytest客户端.py", ["--negative"], "expect_pass"),
    ("第 8 步 SPA：类型 / 构建 / 逻辑单测（应全绿）",
     "自检_SPA登录.py", [], "expect_pass"),
    ("第 8 步 SPA：反向验证（每个破坏点都要被抓到）",
     "自检_SPA登录.py", ["--negative"], "expect_pass"),
]


def run(rel, args):
    """跑一个自检脚本。

    必须给每项单独设超时：外层若被整体 kill，各项会以"无输出 + 非零退出"的形式
    集体报错，看起来像回归，其实是测量被打断。曾因此误判过一次 19/23。
    """
    try:
        proc = subprocess.run([sys.executable, os.path.join(HERE, rel)] + args,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=900,
                              cwd=os.path.dirname(os.path.join(HERE, rel)))
    except subprocess.TimeoutExpired:
        return -1, f"TIMEOUT: {rel} {' '.join(args)} 超过 900 秒未结束"
    out = (proc.stdout or "") + (proc.stderr or "")
    if not out.strip():
        out = f"(无任何输出，exit={proc.returncode} —— 可能被外部中断)"
    return proc.returncode, out


def main():
    quick = "--quick" in sys.argv
    rows, bad = [], 0
    for label, rel, args, expect in SUITES:
        if quick and "--negative" in args:
            continue
        if quick and rel == "自检_SPA登录.py":
            # 冒烟时跳过 vite build（最慢一步）。构建失败是真实交付事故，
            # 所以完整验收里它必须跑——这也是 --quick 不等于验收通过的又一条理由。
            args = args + ["--no-build"]
        code, out = run(rel, args)
        ok = (code != 0) if expect == "expect_fail" else (code == 0)
        rows.append((label, ok, code, out.strip().splitlines()[-1:]))
        if not ok:
            bad += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            for line in out.strip().splitlines()[-12:]:
                print("       " + line)

    print("\n" + "=" * 60)
    print(f"{len(rows) - bad} / {len(rows)} 项验收通过")
    if bad:
        print("有验收未通过，不要进入下一步。")
    elif quick:
        print("冒烟通过（跳过了反向验证）。这**不等于**验收通过 —— "
              "只能说明当前实现没退化，")
        print("说明不了这些断言是否真的在约束什么。合库前、交付前请跑："
              "python verify.py")
    else:
        print("当前已完成的步骤全部通过，且反向验证证明这些断言有约束力。")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        from pnvs_console import setup_console
        setup_console()
    except ImportError:
        pass
    raise SystemExit(main())
