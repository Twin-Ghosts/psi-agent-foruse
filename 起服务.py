# -*- coding: utf-8 -*-
"""本地起移植后的 auth 服务，供契约测试打。

默认开测试钩子（契约测试要取 mock 验证码、要查供应商调用次数）。
**这只是本地验证入口，生产走 uvicorn + 环境变量，钩子默认关闭。**
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("AUTH_DB_PATH", ":memory:")
os.environ.setdefault("AUTH_TEST_HOOKS", "true")
os.environ.setdefault("AUTH_LOG_LEVEL", "WARNING")
os.environ.setdefault("EMAIL_CODE_SALT", "selfcheck-only-salt")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    uvicorn.run(
        "psi_cloud.auth.app:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
