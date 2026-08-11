"""pytest 共享配置。

让 ``tests/`` 在**未安装包**的情况下也能 import ``psi_agent``: 把 ``src`` 放进
sys.path。正常开发流程是 ``pip install -e .``, 但那要求仓库有 git 元数据
(``hatch-vcs`` 从 tag 推版本号), 在只拿到源码压缩包的环境里装不上。

若仓库已按正常方式安装, 这段是无害的 —— sys.path 里多一项已存在的目录。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
