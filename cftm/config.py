"""应用配置的加载与持久化"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

APP_ID = "uk._92li.cftm.CloudflaredTunnelManager"
APP_NAME = "Cloudflared 隧道管理器"
PROJECT_URL = "https://github.com/lingyicute/Cloudflared-Helper-Desktop"

def _resource_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parent.parent

def _find_data_dir() -> Path:
    """查找 data 目录，兼容 PyInstaller、源码运行、Flatpak 等多种布局。"""
    candidates: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidates.append(Path(frozen_root) / "data")
    # 源码运行：repo_root / data
    candidates.append(Path(__file__).resolve().parent.parent / "data")
    # Flatpak / 系统安装： /app/share/cftm/data, /app/share/cloudflared-tunnel-manager/data, /usr/share/...
    candidates.append(Path("/app/share/cftm/data"))
    candidates.append(Path("/app/share/cloudflared-tunnel-manager/data"))
    candidates.append(Path(__file__).resolve().parent / "data")
    for cand in candidates:
        if cand.is_dir():
            return cand
    # 回退：即使不存在也返回首选路径，保持旧行为，调用方再通过 is_file() 判断
    return _resource_root() / "data"

RESOURCE_DATA_DIR = _find_data_dir()
APP_ICON_FILE = RESOURCE_DATA_DIR / f"{APP_ID}.svg"
# 兜底：如果首选路径下图标不存在，尝试在常见系统图标路径中查找（Flatpak 已安装到 hicolor 时）
if not APP_ICON_FILE.is_file():
    for alt in (
        Path("/app/share/icons/hicolor/scalable/apps") / f"{APP_ID}.svg",
        Path("/usr/share/icons/hicolor/scalable/apps") / f"{APP_ID}.svg",
    ):
        if alt.is_file():
            APP_ICON_FILE = alt
            break

def _xdg(env: str, default: str) -> Path:
    value = (os.environ.get(env) or "").strip()
    path = Path(value) if value else None
    return path if path is not None and path.is_absolute() else Path.home() / default


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "cloudflared-tunnel-manager"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "cloudflared-tunnel-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS: dict[str, Any] = {
    # "latest-installed" | "system" | 具体版本号如 "2025.1.0"
    "active_version": "latest-installed",
    "tunnels": [],
    "window": {"width": 980, "height": 660},
    "clipboard_quick_tunnel": True,
}

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"", "0", "false", "no", "off"})


def strict_bool(value: Any, default: bool = False) -> bool:
    """解析 config.json 里可能被手改过的开关值。

    bool("false") 是 True：只认布尔、数字和明确的真/假字符串，
    null、对象、列表及认不出的字符串一律退回 ``default``。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # json 能解析出 NaN，而 bool(nan) 是 True；NaN 不等于自身，按“认不出”处理
        return bool(value) if value == value else default
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    return default


class Config:
    """极简的 JSON 配置对象。"""

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self.data: dict[str, Any] = json.loads(json.dumps(DEFAULTS))
        self.load()

    def load(self) -> None:
        self.data = json.loads(json.dumps(DEFAULTS))
        try:
            # utf-8-sig：有些编辑器保存时会加 BOM，json 遇到 BOM 直接报错，
            # 整份配置会被当成损坏文件搁置、隧道列表全部消失。
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update(loaded)
            else:
                self._set_aside("顶层不是 JSON 对象")
        except FileNotFoundError:
            pass
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            # RecursionError：嵌套过深（比如文件损坏成一长串 "["）时 json 抛的不是
            # JSONDecodeError，漏接会让应用启动即崩溃。
            print(f"[config] 读取配置失败，使用默认值: {exc}")
            self._set_aside(str(exc))
        except OSError as exc:
            print(f"[config] 读取配置失败，使用默认值: {exc}")

    def _set_aside(self, reason: str) -> None:
        """把读不懂的配置文件改名保留。

        否则下一次 save() 会用默认值覆盖它，手改出错（比如多了个逗号）的整份隧道
        配置就此丢失，用户连修一下再放回去的机会都没有。改名保留原文件权限（0600）。
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = f"{self.path.name}.corrupt-{stamp}"
        backup = self.path.with_name(base)
        # 无论同一秒已有多少备份（包括悬空符号链接），都不能覆盖旧文件。
        n = 0
        while backup.exists() or backup.is_symlink():
            n += 1
            backup = self.path.with_name(f"{base}-{n}")
        try:
            os.replace(self.path, backup)
            print(f"[config] 原配置已保留为 {backup}（{reason}）")
        except OSError as exc:
            print(f"[config] 无法保留原配置: {exc}")

    def save(self) -> None:
        tmp: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 同目录原子替换；独占创建随机临时文件，避免固定 .tmp 的符号链接和并发覆盖。
            # NamedTemporaryFile 默认权限 0600，额外参数可能含 Access 凭据，不应公开读取。
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as fh:
                tmp = Path(fh.name)
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(self.path)
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            print(f"[config] 保存配置失败: {exc}")
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
