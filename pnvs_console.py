# -*- coding: utf-8 -*-
"""控制台编码修正。Windows 默认代码页是 GBK/936，直接 print 中文会乱码。

在任何脚本最开头调用一次 setup_console()，即可不依赖 PYTHONUTF8 环境变量
（PowerShell 与 cmd 设环境变量的语法还不一样，容易踩坑）。
"""

import sys


def setup_console():
    """把 stdout/stderr 切成 UTF-8。已经是 UTF-8 或不支持时静默跳过。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass    # 被重定向到不可重配置的目标时无所谓，跳过
