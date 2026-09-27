"""SlayaTheSpire agent：主循环 / 桥接 / Laya 客户端 / 采集 / 观战面板。

`spire-core` 与 `spire-agent` 是两个独立的 src-layout 包（core 要能整包搬到远端），
所以这里做一个最小的路径兜底：没装 `spire-core` 时按仓库相对位置找它。
这样 `python -m spire_agent` 在没 `pip install` 的机器上也能直接跑。
"""

from __future__ import annotations

import pathlib
import sys

_PACKAGES = pathlib.Path(__file__).resolve().parents[3]
_CORE_SRC = _PACKAGES / "spire-core" / "src"
if _CORE_SRC.is_dir() and str(_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_CORE_SRC))

__all__ = ["__version__"]
__version__ = "0.1.0"