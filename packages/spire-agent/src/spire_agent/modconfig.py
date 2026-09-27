"""读写模组的 `SpireConfig` 文件（只有 `host` / `port`）。

MTS 的 `SpireConfig(modID, fileName)` 落在
`ConfigUtils.CONFIG_DIR/<modID>/<fileName>.properties`：

| 系统 | CONFIG_DIR |
|---|---|
| Windows | `%LOCALAPPDATA%\\ModTheSpire` |
| Linux | `~/.config/ModTheSpire` |
| macOS | `~/Library/Preferences/ModTheSpire` |

**模组没有自己的模式。** `mode` 与 `watchdog_sec` 由 agent 在握手后通过 `configure`
帧推送（协议 v2），所以这里只剩监听地址 —— 它有先后依赖（要先有端口才能连上），
推不了，只能落在文件里。历史遗留的 `observe_human` / `watchdog_sec` 键模组已经不读，
留着无害，`stale_keys()` 会替你点出来。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "CONFIG_FILE",
    "DEPRECATED_KEYS",
    "MOD_ID",
    "SUPPORTED_KEYS",
    "config_dir",
    "properties_path",
    "read_properties",
    "set_property",
    "stale_keys",
]

MOD_ID = "spireagent"
CONFIG_FILE = "SlayaTheSpire"

# 模组真正会读的键（`SpireAgentMod` 构造器），其余一概不看。
SUPPORTED_KEYS = ("host", "port")
# 协议 v1 的遗留键：曾经"必须在模组侧手工设置"，现在由 agent 推送。
DEPRECATED_KEYS = ("observe_human", "watchdog_sec")


def config_dir() -> Path:
    """MTS 的配置目录（与 `ConfigUtils.CONFIG_DIR` 对齐）。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "ModTheSpire"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / "ModTheSpire"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "ModTheSpire"


def properties_path(mod_id: str = MOD_ID, config_file: str = CONFIG_FILE) -> Path:
    return config_dir() / mod_id / f"{config_file}.properties"


def read_properties(path: Path | None = None) -> dict[str, str]:
    """按 `java.util.Properties` 的常见形态解析（`#`/`!` 注释、`=`/`:` 分隔、空行）。"""
    path = path or properties_path()
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#!":
            continue
        cut = len(line)
        for sep in ("=", ":"):
            at = line.find(sep)
            if at != -1:
                cut = min(cut, at)
        if cut == len(line):
            key, value = line, ""
        else:
            key, value = line[:cut], line[cut + 1 :]
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def set_property(key: str, value: str, path: Path | None = None) -> Path:
    """写入一个键：同名覆盖，没有就追加；其余行（含注释）原样保留。

    `key` 加锁在最前面；文件不存在时新建（父目录一并建好）。
    """
    path = path or properties_path()
    lines: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    entry = f"{key}={value}"
    replaced = False
    for i, raw in enumerate(lines):
        head = raw.strip()
        if not head or head[0] in "#!":
            continue
        cut = len(head)
        for sep in ("=", ":"):
            at = head.find(sep)
            if at != -1:
                cut = min(cut, at)
        if head[:cut].strip() == key:
            lines[i] = entry
            replaced = True
            break
    if not replaced:
        lines.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def stale_keys(path: Path | None = None) -> list[str]:
    """文件里存在但模组已经不读的键（含大小写/历史遗留）。"""
    props = read_properties(path)
    return sorted(
        key
        for key in props
        if key not in SUPPORTED_KEYS and key.lower() in DEPRECATED_KEYS
    )
