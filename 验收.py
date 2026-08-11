# -*- coding: utf-8 -*-
"""移植后的统一验收入口。

复用 psi-agent-auth 的原版自检（经 `跑自检.py` 的模块别名指向移植后的模块），
不复制任何一条断言 —— 断言集必须与 psi-agent-auth 同一份，否则「移植后全绿」
证明不了移植是对的。

用法：
    python 验收.py            # 全部（正向 + 反向）
    python 验收.py --quick    # 只跑正向
"""

from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# (标签, 脚本, 参数)
SUITES: list[tuple[str, str, list[str]]] = [
    ("schema：约束与清理", "跑自检.py", ["自检_schema.py"]),
    ("schema：反向验证", "跑自检.py", ["自检_schema.py", "--negative"]),
    ("服务层：库内性质", "跑自检.py", ["自检_服务层.py"]),
    ("服务层：反向验证", "跑自检.py", ["自检_服务层.py", "--negative"]),
    ("限频：顺序/归一化/并发", "跑自检.py", ["自检_限频.py"]),
    ("限频：反向验证", "跑自检.py", ["自检_限频.py", "--negative"]),
    ("邀请码：默认关闭/拦截/并发", "跑自检.py", ["自检_邀请码.py"]),
    ("邀请码：反向验证", "跑自检.py", ["自检_邀请码.py", "--negative"]),
    ("供应商适配：签名/两层判据/分发", "跑自检.py", ["自检_供应商.py"]),
    ("供应商适配：反向验证", "跑自检.py", ["自检_供应商.py", "--negative"]),
    ("契约一致：错误码表 + 路由表", "自检_契约一致.py", []),
    ("契约一致：反向验证", "自检_契约一致.py", ["--negative"]),
]


def run_one(label: str, script: str, args: list[str]) -> bool:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, script), *args],
        cwd=HERE, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900,
    )
    ok = proc.returncode == 0
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        tail = (proc.stdout or "").strip().splitlines()[-25:]
        for line in tail:
            print(f"       {line}")
        err = (proc.stderr or "").strip().splitlines()[-10:]
        for line in err:
            print(f"       ! {line}")
    return ok


def main() -> int:
    quick = "--quick" in sys.argv
    suites = [s for s in SUITES if not (quick and "--negative" in s[2])]

    results = [run_one(*s) for s in suites]
    passed = sum(results)
    total = len(results)

    print("\n" + "=" * 60)
    print(f"{passed} / {total} 项验收通过")
    if passed != total:
        print("有项目未通过，上面是失败输出的尾部。")
        return 1
    print("移植后的 psi-cloud 与 psi-agent-auth 断言等价，且反向验证证明断言有约束力。")
    print("\n注意：契约测试（60 项）需要起服务后另跑：")
    print("  python 起服务.py 8099")
    print("  python ../psi-agent-auth/contract/契约测试.py --base http://127.0.0.1:8099")
    print("尚未验证：真实收到邮件 / 短信 —— 需真实凭据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
