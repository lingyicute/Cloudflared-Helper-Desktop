"""隧道配置模型与 cloudflared 子进程管理"""
from __future__ import annotations

import shlex
import signal
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject  # noqa: E402

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_ERROR = "error"

STATE_LABELS = {
    STATE_STOPPED: "已停止",
    STATE_STARTING: "正在连接…",
    STATE_RUNNING: "已连接",
    STATE_ERROR: "连接失败",
}

# cloudflared access 子命令
MODES = [
    ("tcp", "TCP（通用）"),
    ("ssh", "SSH"),
    ("rdp", "RDP 远程桌面"),
    ("smb", "SMB 文件共享"),
]


@dataclass
class TunnelConfig:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = ""
    hostname: str = ""
    port: int = 21128
    listen_host: str = "127.0.0.1"
    mode: str = "tcp"
    autostart: bool = False
    extra_args: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TunnelConfig":
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}  # type: ignore[attr-defined]
        cfg = cls(**known)
        try:
            cfg.port = int(cfg.port)
        except (TypeError, ValueError):
            cfg.port = 21128
        return cfg

    def copy(self) -> "TunnelConfig":
        return TunnelConfig.from_dict(self.to_dict())

    @property
    def display_name(self) -> str:
        return self.name.strip() or self.hostname or "未命名隧道"

    def build_argv(self, binary: str) -> list[str]:
        argv = [
            binary,
            "access",
            self.mode or "tcp",
            "--hostname",
            self.hostname,
            "--url",
            f"{self.listen_host or '127.0.0.1'}:{self.port}",
        ]
        if self.extra_args.strip():
            argv += shlex.split(self.extra_args)
        return argv

    def command_string(self, binary: str = "cloudflared") -> str:
        return " ".join(shlex.quote(a) for a in self.build_argv(binary))


class TunnelProcess(GObject.Object):
    """单个隧道对应的 cloudflared 子进程。"""

    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "log-line": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, config: TunnelConfig):
        super().__init__()
        self.config = config
        self.state = STATE_STOPPED
        self.last_error: Optional[str] = None
        self.started_at: Optional[float] = None
        self._log: deque[str] = deque(maxlen=3000)
        self._proc: Optional[Gio.Subprocess] = None
        self._stream: Optional[Gio.DataInputStream] = None
        self._stopping = False

    # ------------------------------------------------------------ 属性
    def is_active(self) -> bool:
        return self.state in (STATE_STARTING, STATE_RUNNING)

    def log_lines(self) -> list[str]:
        return list(self._log)

    def clear_log(self) -> None:
        self._log.clear()

    def uptime(self) -> float:
        return time.monotonic() - self.started_at if self.started_at and self.is_active() else 0.0

    # ------------------------------------------------------------ 内部
    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.emit("state-changed", state)

    def _append(self, line: str) -> None:
        stamped = f"[{time.strftime('%H:%M:%S')}] {line}"
        self._log.append(stamped)
        self.emit("log-line", stamped)

    # ------------------------------------------------------------ 控制
    def start(self, binary: str) -> None:
        if self.is_active():
            return
        argv = self.config.build_argv(binary)
        self.last_error = None
        self._stopping = False
        self._set_state(STATE_STARTING)
        self._append("$ " + " ".join(shlex.quote(a) for a in argv))
        try:
            launcher = Gio.SubprocessLauncher.new(
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
            )
            proc = launcher.spawnv(argv)
        except GLib.Error as exc:
            self.last_error = exc.message
            self._append(f"[启动失败] {exc.message}")
            self._set_state(STATE_ERROR)
            return

        self._proc = proc
        self.started_at = time.monotonic()
        self._stream = Gio.DataInputStream.new(proc.get_stdout_pipe())
        self._read_next(self._stream)
        proc.wait_async(None, self._on_exit)
        GLib.timeout_add(2500, self._promote_running, proc)

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._stopping = True
        self._append("[正在断开…]")
        try:
            proc.send_signal(signal.SIGTERM)
        except Exception:  # noqa: BLE001  (非 Unix 平台)
            proc.force_exit()
        GLib.timeout_add_seconds(5, self._force_kill, proc)

    # ------------------------------------------------------------ 回调
    def _force_kill(self, proc: Gio.Subprocess) -> bool:
        if self._proc is proc:
            self._append("[进程未响应，强制结束]")
            proc.force_exit()
        return False

    def _promote_running(self, proc: Gio.Subprocess) -> bool:
        if self._proc is proc and self.state == STATE_STARTING:
            self._set_state(STATE_RUNNING)
        return False

    def _read_next(self, stream: Gio.DataInputStream) -> None:
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line, stream)

    def _on_line(self, stream: Gio.DataInputStream, result, _data) -> None:
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if line is None or stream is not self._stream:
            return
        text = line.rstrip("\r\n")
        if text:
            self._append(text)
            low = text.lower()
            if "start websocket listener" in low or "listening on" in low:
                self._set_state(STATE_RUNNING)
            if " err " in f" {low} " or "level=error" in low or "error" in low[:40]:
                self.last_error = text
        self._read_next(stream)

    def _on_exit(self, proc: Gio.Subprocess, result) -> None:
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if proc is not self._proc:
            return
        code = proc.get_exit_status() if proc.get_if_exited() else -1
        self._append(f"[进程已退出，状态码 {code}]")
        self._proc = None
        self._stream = None
        self.started_at = None
        if self._stopping or code == 0:
            self._set_state(STATE_STOPPED)
        else:
            self._set_state(STATE_ERROR)
        self._stopping = False


class TunnelManager(GObject.Object):
    """保存全部隧道，负责持久化与批量操作。"""

    __gsignals__ = {
        "tunnels-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.procs: list[TunnelProcess] = []
        for item in config.get("tunnels", []) or []:
            self._wrap(TunnelConfig.from_dict(item))

    def _wrap(self, cfg: TunnelConfig) -> TunnelProcess:
        proc = TunnelProcess(cfg)
        proc.connect("state-changed", lambda p, st: self.emit("state-changed", p.config.id, st))
        self.procs.append(proc)
        return proc

    def save(self) -> None:
        self.config.set("tunnels", [p.config.to_dict() for p in self.procs])
        self.config.save()

    # ------------------------------------------------------------ CRUD
    def get(self, tunnel_id: str) -> Optional[TunnelProcess]:
        return next((p for p in self.procs if p.config.id == tunnel_id), None)

    def add(self, cfg: TunnelConfig) -> TunnelProcess:
        proc = self._wrap(cfg)
        self.save()
        self.emit("tunnels-changed")
        return proc

    def update(self, cfg: TunnelConfig) -> None:
        proc = self.get(cfg.id)
        if proc is None:
            self.add(cfg)
            return
        proc.config = cfg
        self.save()
        self.emit("tunnels-changed")

    def remove(self, tunnel_id: str) -> None:
        proc = self.get(tunnel_id)
        if proc is None:
            return
        proc.stop()
        self.procs.remove(proc)
        self.save()
        self.emit("tunnels-changed")

    def duplicate(self, tunnel_id: str) -> Optional[TunnelProcess]:
        proc = self.get(tunnel_id)
        if proc is None:
            return None
        cfg = proc.config.copy()
        cfg.id = uuid.uuid4().hex[:10]
        cfg.name = f"{proc.config.display_name} (副本)"
        cfg.autostart = False
        return self.add(cfg)

    # ------------------------------------------------------------ 批量
    def active(self) -> list[TunnelProcess]:
        return [p for p in self.procs if p.is_active()]

    def port_conflict(self, cfg: TunnelConfig) -> Optional[TunnelProcess]:
        for p in self.active():
            if p.config.id != cfg.id and p.config.port == cfg.port and p.config.listen_host == cfg.listen_host:
                return p
        return None

    def stop_all(self) -> None:
        for p in self.procs:
            p.stop()
