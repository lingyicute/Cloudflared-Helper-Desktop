"""桌面集成：让任务栏 / Dock 能正确显示应用图标。

GNOME Shell、KDE Plasma 等桌面环境并不直接使用窗口自带的图标，而是通过窗口的
app_id（Wayland）或 WM_CLASS（X11）去查找同名的 .desktop 文件，再从图标主题中加载
其 Icon= 指定的图标。因此当程序没有“安装”到系统里（例如直接 ``python3 main.py``
运行，或使用 PyInstaller 便携包）时，任务栏只能显示默认的齿轮图标。

这里的做法：在非 Flatpak、且系统里没有对应 .desktop 文件时，把
``data/<APP_ID>.svg`` 复制到 ``~/.local/share/icons/hicolor/scalable/apps/``，
并在 ``~/.local/share/applications/`` 生成一份指向当前启动方式的 .desktop 文件。
所有失败都只打印警告，不影响程序运行。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .config import APP_ICON_FILE, APP_ID, RESOURCE_DATA_DIR

DESKTOP_FILE_NAME = f"{APP_ID}.desktop"
# 写入自动生成的 .desktop 文件中的标记，用来区分用户自己放置的文件（不会覆盖）
GENERATED_MARK = "X-CFTM-Generated=true"


def _xdg_data_home() -> Path:
    value = os.environ.get("XDG_DATA_HOME")
    return Path(value) if value else Path.home() / ".local" / "share"


def _xdg_data_dirs() -> list[Path]:
    value = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [Path(p) for p in value.split(":") if p]


def in_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID")) or Path("/.flatpak-info").exists()


def system_desktop_file_exists() -> bool:
    """系统级目录（XDG_DATA_DIRS）里是否已经安装了本应用的 .desktop 文件。"""
    return any((d / "applications" / DESKTOP_FILE_NAME).is_file() for d in _xdg_data_dirs())


def _quote_exec_arg(arg: str) -> str:
    """按 Desktop Entry 规范为 Exec= 中的单个参数加引号。"""
    if arg and not any(c in arg for c in " \t\n\"'\\><~|&;$*?#()`"):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def launch_command() -> str:
    """返回当前程序的启动命令，用于 .desktop 文件的 Exec=。"""
    if getattr(sys, "frozen", False):  # PyInstaller 便携包
        return _quote_exec_arg(os.path.abspath(sys.executable))
    main_py = Path(RESOURCE_DATA_DIR).parent / "main.py"
    return f"{_quote_exec_arg(sys.executable)} {_quote_exec_arg(str(main_py))}"


def _build_desktop_entry() -> str:
    """以 data/ 里的 .desktop 为模板，替换 Exec= 并补充 StartupWMClass=。"""
    template = RESOURCE_DATA_DIR / DESKTOP_FILE_NAME
    lines: list[str] = []
    if template.is_file():
        for line in template.read_text(encoding="utf-8").splitlines():
            if line.startswith(("Exec=", "StartupWMClass=", "X-CFTM-Generated=")):
                continue
            lines.append(line)
    if not lines:
        lines = [
            "[Desktop Entry]",
            "Name=Cloudflared Tunnel Manager",
            "Name[zh_CN]=Cloudflared 隧道管理器",
            f"Icon={APP_ID}",
            "Terminal=false",
            "Type=Application",
            "Categories=Network;RemoteAccess;GTK;GNOME;",
        ]
    lines += [
        f"Exec={launch_command()}",
        f"StartupWMClass={APP_ID}",
        GENERATED_MARK,
    ]
    return "\n".join(lines) + "\n"


def _write_if_changed(path: Path, content: str) -> bool:
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        print(f"[desktop] 写入 {path} 失败: {exc}", file=sys.stderr)
        return False


def install_icon() -> bool:
    """把 SVG 图标复制到用户的 hicolor 主题目录。返回是否发生了更新。"""
    if not APP_ICON_FILE.is_file():
        return False
    target = _xdg_data_home() / "icons" / "hicolor" / "scalable" / "apps" / APP_ICON_FILE.name
    try:
        if target.is_file() and target.read_bytes() == APP_ICON_FILE.read_bytes():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(APP_ICON_FILE, target)
        # 刷新 hicolor 目录的 mtime，让 GTK / 桌面环境放弃过期的 icon-theme.cache
        os.utime(target.parents[2], None)
        return True
    except OSError as exc:
        print(f"[desktop] 安装图标失败: {exc}", file=sys.stderr)
        return False


def install_desktop_file() -> bool:
    """在 ~/.local/share/applications 生成（或更新）.desktop 文件。返回是否发生了更新。"""
    target = _xdg_data_home() / "applications" / DESKTOP_FILE_NAME
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if existing and GENERATED_MARK not in existing:
            return False  # 用户手工放置的文件，不要动
    return _write_if_changed(target, _build_desktop_entry())


def ensure_desktop_integration() -> None:
    """在需要时安装图标与 .desktop 文件，让任务栏能显示应用图标。"""
    if sys.platform == "darwin" or os.name == "nt" or in_flatpak():
        return
    if system_desktop_file_exists():
        return  # 已通过软件包等方式安装到系统，桌面环境自己就能找到图标
    changed = install_icon()
    changed = install_desktop_file() or changed
    if changed:
        print("[desktop] 已更新 ~/.local/share 下的应用图标与 .desktop 文件")
