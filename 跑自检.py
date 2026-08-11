# -*- coding: utf-8 -*-
"""用 psi-agent-auth 的原版自检，验证移植进 psi-cloud 的模块。

**不复制、不改写任何一条断言。** 做法是把 ``app.*`` 这个模块名指向移植后的
``psi_cloud.auth.*``，然后原封不动地执行自检文件。断言集与 psi-agent-auth
完全同一份 —— 否则「移植后仍全绿」证明不了任何东西。

用法：
    python 跑自检.py 自检_服务层.py [--negative]
"""

from __future__ import annotations

import os
import runpy
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PORT_SRC = os.path.join(HERE, "src")
AUTH_REPO = os.path.abspath(os.path.join(HERE, "..", "psi-agent-auth"))


def install_alias() -> None:
    """让 ``import app.xxx`` 落到 psi_cloud.auth.xxx 上。"""
    sys.path.insert(0, PORT_SRC)

    import psi_cloud.auth as _auth

    pkg = types.ModuleType("app")
    pkg.__path__ = list(_auth.__path__)          # 让 app.xxx 可被正常导入
    sys.modules["app"] = pkg

    # service 内部是 `from . import providers_core as providers`，自检却按
    # `app.providers` 导入 —— 两个名字必须指向同一个模块对象，否则自检改的是
    # 一份、service 读的是另一份，破坏点会失效（这正是反向验证要防的情形）。
    from psi_cloud.auth import providers_core, service, store

    sys.modules["app.providers"] = providers_core
    sys.modules["app.service"] = service
    sys.modules["app.store"] = store
    pkg.providers = providers_core
    pkg.service = service
    pkg.store = store

    try:
        from psi_cloud.auth import real_providers
    except ImportError:
        pass                                     # aiohttp 缺失时该自检自己会报
    else:
        sys.modules["app.real_providers"] = real_providers
        pkg.real_providers = real_providers


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python 跑自检.py <自检文件名> [自检参数...]")
        return 2

    target = os.path.join(AUTH_REPO, sys.argv[1])
    if not os.path.exists(target):
        print(f"找不到自检文件：{target}")
        return 2

    install_alias()

    # 自检文件自己会 sys.path.insert(0, 它所在目录)，那会把 psi-agent-auth 的
    # app/ 变成可导入 —— 但 sys.modules 里的别名已先占位，import 不会再去找它。
    sys.argv = [target, *sys.argv[2:]]
    try:
        runpy.run_path(target, run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
