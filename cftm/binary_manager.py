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
import time
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

def format_size(num: float) -> str:
    """把字节数格式化为人类可读的字符串，如「12.3 MB」。"""
    num = float(num)
    for unit in ("B", "KB", "MB"):
        if num < 1024:
            return f"{num:.0f} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def format_speed(bytes_per_sec: float) -> str:
    """把下载速度格式化为「x.x MB/s」。"""
    return f"{format_size(bytes_per_sec)}/s"


def format_eta(seconds: float) -> str:
    """把剩余秒数格式化为「剩余 1 分 05 秒」这样的文本。"""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"剩余 {seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"剩余 {minutes} 分 {seconds:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"剩余 {hours} 小时 {minutes:02d} 分"

class BinaryManager:
    def __init__(self, versions_dir: Path):
        self.versions_dir = Path(versions_dir)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.system, self.arch = detect_platform()
        self.asset = asset_name(self.system, self.arch)
        self._active_downloads: dict[str, bool] = {}  # tag -> cancel 标志
        self._download_stats: dict[str, dict] = {}  # tag -> 下载统计（字节数 / 速度 / 剩余时间）

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

    def download_stats(self, tag: str) -> Optional[dict]:
        """正在进行的下载统计：{"got", "total", "speed", "eta"}；没有则返回 None。"""
        return self._download_stats.get(tag)

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
        self._download_stats.pop(tag, None)

        def cleanup(dir_path: Path, *files: Path) -> None:
            """清掉残留文件；目录空了就移除，别留下“这个版本已安装”的假象。"""
            for path in files:
                try:
                    if path.is_file():
                        path.unlink()
                except OSError:
                    pass
            try:
                if dir_path.exists() and not any(dir_path.iterdir()):
                    dir_path.rmdir()
            except OSError:
                pass

        def finish(error: Optional[str]) -> None:
            """注销下载状态，再通知 UI。

            顺序很重要：如果先通知 UI、再在 finally 里注销，主线程有机会在两步之间
            跑掉 ``_sync_release_rows``，此时 ``is_downloading()`` 仍为真，那一行会被
            跳过刷新，进度条永远停在最后一次进度上。
            """
            self._active_downloads.pop(tag, None)
            self._download_stats.pop(tag, None)
            GLib.idle_add(done_cb, tag, error)

        def work() -> None:
            dest_dir = self.versions_dir / tag
            tmp = dest_dir / (self.exe_name + ".part")
            final = self.path_for(tag)
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                url = f"{DOWNLOAD_BASE}/{tag}/{self.asset}"
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as fh:
                    total = int(resp.headers.get("Content-Length") or 0)
                    got = 0
                    started = time.monotonic()
                    last_emit = 0.0  # 上次向 UI 汇报进度的时刻
                    win_t, win_got = started, 0  # 速度采样窗口的起点
                    speed = 0.0
                    while True:
                        if self._active_downloads.get(tag):
                            raise DownloadCancelled()
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        now = time.monotonic()
                        if now - win_t >= 1.0:
                            # 每约 1 秒重新采样一次瞬时速度，避免数字剧烈抖动
                            speed = (got - win_got) / (now - win_t)
                            win_t, win_got = now, got
                        elif win_got == 0 and now > started:
                            speed = got / (now - started)  # 第一秒内先用平均速度
                        eta = (total - got) / speed if total and speed > 0 else -1.0
                        self._download_stats[tag] = {"got": got, "total": total, "speed": speed, "eta": eta}
                        # 最多每 100 ms 刷新一次 UI，避免大量 idle 回调拖慢主线程
                        if now - last_emit >= 0.1:
                            last_emit = now
                            GLib.idle_add(progress_cb, tag, (got / total) if total else -1.0)
                    # 连接被中途切断时 read() 也只是返回空串，必须自己核对字节数，
                    # 否则半截文件会被当成可用的 cloudflared 安装进去。
                    if total and got != total:
                        raise RuntimeError(f"下载不完整（{got}/{total} 字节），请重试")
                    elapsed = max(time.monotonic() - started, 1e-6)
                    self._download_stats[tag] = {"got": got, "total": total, "speed": got / elapsed, "eta": 0.0}
                    GLib.idle_add(progress_cb, tag, 1.0 if total else -1.0)

                if self.asset.endswith(".tgz"):
                    with tarfile.open(tmp, "r:gz") as tf:
                        member = next(
                            (m for m in tf.getmembers() if m.name.rstrip("/").endswith("cloudflared")), None
                        )
                        if member is None:
                            raise RuntimeError(f"{self.asset} 的压缩包里没有找到 cloudflared")
                        src = tf.extractfile(member)
                        if src is None:
                            raise RuntimeError(f"{self.asset} 里的 cloudflared 无法读取")
                        with src, open(final, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    tmp.unlink()
                else:
                    tmp.replace(final)
                if self.system != "windows":
                    final.chmod(0o755)
                finish(None)
            except DownloadCancelled:
                cleanup(dest_dir, tmp, final)
                finish("已取消")
            except Exception as exc:  # noqa: BLE001
                cleanup(dest_dir, tmp, final)
                finish(str(exc) or exc.__class__.__name__)
            finally:
                # 兜底：万一上面任何一步抛异常，也不要把这个 tag 永久卡在“下载中”
                self._active_downloads.pop(tag, None)

        threading.Thread(target=work, daemon=True).start()

    def cancel(self, tag: str) -> None:
        if tag in self._active_downloads:
            self._active_downloads[tag] = True

    def remove(self, tag: str) -> None:
        shutil.rmtree(self.versions_dir / tag, ignore_errors=True)
