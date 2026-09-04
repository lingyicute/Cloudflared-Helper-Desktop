"""应用配置的加载与持久化"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_ID = "uk._92li.cftm.CloudflaredTunnelManager"
APP_NAME = "Cloudflared 隧道管理器"
PROJECT_URL = "https://github.com/lingyicute/Cloudflared-Helper"


def _xdg(env: str, default: str) -> Path:
    value = os.environ.get(env)
    return Path(value) if value else Path.home() / default


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "cloudflared-tunnel-manager"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "cloudflared-tunnel-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS: dict[str, Any] = {
    # "latest-installed" | "system" | 具体版本号如 "2025.1.0"
    "active_version": "latest-installed",
    "tunnels": [],
    "window": {"width": 980, "height": 660},
}


class Config:
    """极简的 JSON 配置对象。"""

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self.data: dict[str, Any] = json.loads(json.dumps(DEFAULTS))
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[config] 读取配置失败，使用默认值: {exc}")

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
        except OSError as exc:
            print(f"[config] 保存配置失败: {exc}")

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
