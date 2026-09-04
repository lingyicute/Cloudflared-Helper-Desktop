"""cloudflared 二进制文件的版本管理。

功能：
- 自动检测系统 / 架构，匹配官方发布的资源文件名
- 从 GitHub Releases 获取版本列表
- 下载、安装（自动 chmod +x）、删除指定版本
- 在“最新已安装 / 系统 PATH / 指定版本”之间解析当前应使用的二进制
"""
from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from gi.repository import GLib

GITHUB_API = "https://api.github.com/repos/cloudflare/cloudflared/releases?per_page=30"
DOWNLOAD_BASE = "https://github.com/cloudflare/cloudflared/releases/download"
USER_AGENT = "cloudflared-tunnel-manager/1.0 (+https://github.com)"


class DownloadCancelled(Exception):
    pass


def detect_platform() -> tuple[str, str]:
    """返回 (system, arch)，arch 使用 cloudflared 发布时的命名。"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine.startswith("armv") or machine == "arm":
        arch = "arm"
    elif machine in ("i386", "i686", "x86"):
        arch = "386"
    else:
        arch = machine
    return system, arch


def asset_name(system: str, arch: str) -> str:
    if system == "darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    if system == "windows":
        return f"cloudflared-windows-{arch}.exe"
    return f"cloudflared-linux-{arch}"


def version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", tag)) or (0,)


class BinaryManager:
    def __init__(self, versions_dir: Path):
        self.versions_dir = Path(versions_dir)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.system, self.arch = detect_platform()
        self.asset = asset_name(self.system, self.arch)
        self._active_downloads: dict[str, bool] = {}  # tag -> cancel 标志

    # ------------------------------------------------------------------ 路径
    @property
    def exe_name(self) -> str:
        return "cloudflared.exe" if self.system == "windows" else "cloudflared"

    def path_for(self, tag: str) -> Path:
        return self.versions_dir / tag / self.exe_name

    def installed(self) -> list[str]:
        """已安装版本标签，按版本号从新到旧排序。"""
        tags = []
        for child in self.versions_dir.iterdir() if self.versions_dir.exists() else []:
            if child.is_dir() and (child / self.exe_name).exists():
                tags.append(child.name)
        return sorted(tags, key=version_key, reverse=True)

    def is_installed(self, tag: str) -> bool:
        return self.path_for(tag).exists()

    def is_downloading(self, tag: str) -> bool:
        return tag in self._active_downloads

    def system_binary(self) -> Optional[str]:
        return shutil.which("cloudflared")

    def resolve(self, active: str) -> Optional[str]:
        """根据配置解析出应使用的二进制路径；找不到返回 None。"""
        if active == "system":
            return self.system_binary()
        if active == "latest-installed":
            inst = self.installed()
            if inst:
                return str(self.path_for(inst[0]))
            return self.system_binary()
        path = self.path_for(active)
        if path.exists():
            return str(path)
        # 回退：指定的版本已被删除
        inst = self.installed()
        return str(self.path_for(inst[0])) if inst else self.system_binary()

    # ------------------------------------------------------------------ 探测
    @staticmethod
    def probe_version(path: str) -> Optional[str]:
        """运行 `cloudflared --version` 并返回精简的版本字符串。"""
        try:
            out = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=8
            )
            text = (out.stdout or out.stderr).strip()
            match = re.search(r"version\s+(\S+)", text)
            return match.group(1) if match else (text.splitlines()[0] if text else None)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ 远程
    def fetch_releases(self, callback: Callable[[Optional[list], Optional[str]], None]) -> None:
        """异步获取版本列表，回调在 GTK 主线程执行：callback(releases, error)。"""

        def work() -> None:
            try:
                req = urllib.request.Request(
                    GITHUB_API,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.load(resp)
                releases = []
                for rel in data:
                    names = {a.get("name") for a in rel.get("assets", [])}
                    releases.append(
                        {
                            "tag": rel.get("tag_name", ""),
                            "published": (rel.get("published_at") or "")[:10],
                            "prerelease": bool(rel.get("prerelease")),
                            "has_asset": self.asset in names,
                        }
                    )
                GLib.idle_add(callback, releases, None)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(callback, None, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def download(
        self,
        tag: str,
        progress_cb: Callable[[str, float], None],
        done_cb: Callable[[str, Optional[str]], None],
    ) -> None:
        """异步下载并安装某个版本。progress 为 0~1，未知大小时为 -1。"""
        if tag in self._active_downloads:
            return
        self._active_downloads[tag] = False

        def cleanup(dest_dir: Path, tmp: Path) -> None:
            try:
                if tmp.exists():
                    tmp.unlink()
                if dest_dir.exists() and not any(dest_dir.iterdir()):
                    dest_dir.rmdir()
            except OSError:
                pass

        def work() -> None:
            dest_dir = self.versions_dir / tag
            tmp = dest_dir / (self.exe_name + ".part")
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                url = f"{DOWNLOAD_BASE}/{tag}/{self.asset}"
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as fh:
                    total = int(resp.headers.get("Content-Length") or 0)
                    got = 0
                    while True:
                        if self._active_downloads.get(tag):
                            raise DownloadCancelled()
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        GLib.idle_add(progress_cb, tag, (got / total) if total else -1.0)

                final = self.path_for(tag)
                if self.asset.endswith(".tgz"):
                    with tarfile.open(tmp, "r:gz") as tf:
                        member = next(
                            m for m in tf.getmembers() if m.name.rstrip("/").endswith("cloudflared")
                        )
                        src = tf.extractfile(member)
                        if src is None:
                            raise RuntimeError("压缩包中未找到 cloudflared")
                        with src, open(final, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    tmp.unlink()
                else:
                    tmp.replace(final)
                if self.system != "windows":
                    final.chmod(0o755)
                GLib.idle_add(done_cb, tag, None)
            except DownloadCancelled:
                cleanup(dest_dir, tmp)
                GLib.idle_add(done_cb, tag, "已取消")
            except Exception as exc:  # noqa: BLE001
                cleanup(dest_dir, tmp)
                GLib.idle_add(done_cb, tag, str(exc))
            finally:
                self._active_downloads.pop(tag, None)

        threading.Thread(target=work, daemon=True).start()

    def cancel(self, tag: str) -> None:
        if tag in self._active_downloads:
            self._active_downloads[tag] = True

    def remove(self, tag: str) -> None:
        shutil.rmtree(self.versions_dir / tag, ignore_errors=True)
