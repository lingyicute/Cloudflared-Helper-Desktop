"""隧道配置模型与 cloudflared 子进程管理"""
from __future__ import annotations

import ipaddress
import re
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

from .config import strict_bool  # noqa: E402

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

_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_RE = re.compile(rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})+")
_IPV6_CHARS_RE = re.compile(r"[0-9A-Fa-f:.]+")
_NUMERIC_LABEL_RE = re.compile(r"[0-9]+|0[xX][0-9A-Fa-f]+")
# 允许空十六进制数字的 "0x"：只在整个主机名都由数字/十六进制标签组成时才按 IP 变体处理
_IP_LIKE_LABEL_RE = re.compile(r"[0-9]+|0[xX][0-9A-Fa-f]*")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_HOSTNAME_IP_MSG = "cloudflared 不支持 IP 地址，请使用域名"
_HOSTNAME_FORM_MSG = "需为完整域名（如 ssh.example.com），不含协议、路径或端口"


# cloudflared 控制台日志的行首：ISO 时间戳 + 三字母级别
_LOG_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.+Z-]+\s+(?:(?:ERR|FTL|WRN|INF|DBG|TRC)\s+)?")


def summarize_error(line: str) -> str:
    """把一行错误日志压成适合放进提示条的短句。

    提示条只容得下几十个字：原样截断的话前半截全是时间戳、级别和套话，
    真正的原因（多半在行尾的 error="…" 里）反而被截掉。
    """
    low = line.lower()
    if "address already in use" in low:
        return "本地端口已被其他程序占用 (address already in use)"
    if "bind: permission denied" in low:
        return "没有权限监听该端口 (permission denied)"
    return _LOG_PREFIX_RE.sub("", line.strip(), count=1)


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return str(signum)


def normalize_hostname(raw: object) -> str:
    """把用户输入整理成隧道主机名；无效时抛 ValueError。

    规则与 cf-helper.sh 一致：RFC 1123（标签 1-63 字符、总长不超过 253、不含下划线）、
    至少两段；自动剥离 scheme、路径/查询/片段、:port 与末尾点号。
    cloudflared 不支持 IP 地址，IPv4/IPv6 及其数字变体一律拒绝。
    """
    if raw is None:
        raise ValueError("隧道主机名不能为空")
    if not isinstance(raw, str):
        raise ValueError(_HOSTNAME_FORM_MSG)
    if not raw.strip():
        raise ValueError("隧道主机名不能为空")
    stripped = raw.strip()
    had_scheme = bool(_SCHEME_RE.match(stripped))
    host = _SCHEME_RE.sub("", stripped, count=1)
    host = re.split(r"[/?#]", host, maxsplit=1)[0]
    if had_scheme and "@" in host:
        # 只有带协议的 URL 才有 userinfo（https://user:pw@host）；裸 user@host 仍然拒绝。
        host = host.rsplit("@", 1)[-1]
    if host.startswith("[") or (host.count(":") >= 2 and _IPV6_CHARS_RE.fullmatch(host)):
        raise ValueError(_HOSTNAME_IP_MSG)
    host = re.sub(r"^(.+):[0-9]+$", r"\1", host)
    if host.endswith("."):
        host = host[:-1]
    labels = host.split(".")
    if _NUMERIC_LABEL_RE.fullmatch(labels[-1]) or all(_IP_LIKE_LABEL_RE.fullmatch(x) for x in labels):
        raise ValueError(_HOSTNAME_IP_MSG)
    if len(host) > 253 or not _HOSTNAME_RE.fullmatch(host):
        raise ValueError(_HOSTNAME_FORM_MSG)
    return host


def validate_hostname(hostname: object) -> str:
    """严格校验已保存的主机名（必须已是规范形式），build_argv 与界面预检共用。"""
    host = normalize_hostname(hostname)
    if host != hostname:
        raise ValueError(_HOSTNAME_FORM_MSG)
    return host


def listener_address(host: str, port: int) -> str:
    """生成 host:port，IPv6 字面量必须加方括号；不接受 URL 或内嵌端口。"""
    if not isinstance(host, str):
        raise ValueError("监听地址应为主机名或 IP 地址")
    host = host.strip() or "127.0.0.1"
    if host.startswith("-") or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in host):
        raise ValueError("监听地址不能以连字符开头或包含空白、控制字符")
    if ":" in host:
        literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
        try:
            ipaddress.IPv6Address(literal)
        except ValueError as exc:
            raise ValueError("监听地址应为主机名或 IP 地址，不能包含协议头或端口") from exc
        host = f"[{literal}]"
    elif any(c in host for c in "/?#@[]\\"):
        raise ValueError("监听地址应为主机名或 IP 地址")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    return f"{host}:{port}"


@dataclass
class TunnelConfig:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = ""
    hostname: str = ""
    port: int = 21128
    listen_host: str = "127.0.0.1"
    autostart: bool = False
    extra_args: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TunnelConfig":
        # config.json 可能被手改坏：tunnels 里混入字符串/null/数字时 data 不是 dict，
        # 此时必须退回默认对象而不是抛异常（否则整个应用启动即崩溃、没有窗口）。
        if not isinstance(data, dict):
            return cls()
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}  # type: ignore[attr-defined]
        cfg = cls(**known)
        # 手工编辑 / 从别处拷来的 config.json 里 id 可能缺失、为 null 或重复；
        # 非法 id 会让 UI 构造菜单时抛 TypeError（导致启动后没有窗口），
        # 重复 id 会让 get() 永远只命中第一条（编辑/删除作用到别的隧道）。
        if not isinstance(cfg.id, str) or not cfg.id.strip():
            cfg.id = uuid.uuid4().hex[:10]
        else:
            cfg.id = cfg.id.strip()
        if isinstance(cfg.port, bool):
            cfg.port = 21128
        else:
            try:
                cfg.port = int(cfg.port)
            except (TypeError, ValueError, OverflowError):
                cfg.port = 21128
        if not 1 <= cfg.port <= 65535:
            cfg.port = 21128
        for str_field in ("name", "hostname", "listen_host", "extra_args"):
            value = getattr(cfg, str_field)
            if not isinstance(value, str):
                setattr(cfg, str_field, "" if value is None else str(value))
        # bool("false") 是 True：手改配置时把开关写成字符串 "false" 不能反而开启自动连接。
        # 只认明确的真值，其余（null、空字符串、对象、列表）一律视为关闭。
        cfg.autostart = strict_bool(cfg.autostart, default=False)
        # 额外清理：hostname 和 listen_host 去除首尾空白，避免后续校验遗漏
        cfg.hostname = cfg.hostname.strip()
        # 旧版本允许保存 URL 形式（https://host/path）的主机名，升级后自动整理成纯域名，
        # 否则这些隧道会因 build_argv 的严格校验而全部无法启动；整理不了的保持原样，启动时报错。
        if cfg.hostname:
            try:
                cfg.hostname = normalize_hostname(cfg.hostname)
            except ValueError:
                pass
        cfg.listen_host = cfg.listen_host.strip() or "127.0.0.1"
        return cfg

    def copy(self) -> "TunnelConfig":
        return TunnelConfig.from_dict(self.to_dict())

    @property
    def display_name(self) -> str:
        return self.name.strip() or self.hostname or "未命名隧道"

    # cloudflared 的 access tcp/ssh/rdp/smb 是同一个子命令（上游把后三者声明为 tcp 的
    # 别名，四个子命令共用一个 Action），--url 的 scheme 也只影响缺省值不影响行为，
    # 所以这里固定用 tcp，不必再把“协议类型”存进配置。
    def build_argv(self, binary: str) -> list[str]:
        try:
            validate_hostname(self.hostname)
        except ValueError as exc:
            raise ValueError(f"无效的隧道主机名：{exc}") from exc
        argv = [
            binary,
            "access",
            "tcp",
            "--hostname",
            self.hostname,
            "--url",
            listener_address(self.listen_host, self.port),
        ]
        if isinstance(self.extra_args, str) and self.extra_args.strip():
            # extra_args 里引号不配对时 shlex.split 会抛 ValueError，调用方必须捕获
            # （start() 会转为 ERROR 状态，对话框会提前拦截）。
            argv += shlex.split(self.extra_args)
        if any("\x00" in arg for arg in argv):
            raise ValueError("命令行参数不能包含 NUL 字符")
        return argv

    @property
    def listen_label(self) -> str:
        """界面和命令行展示用的 ``host:port``：IPv6 加方括号、空地址按默认值，永不抛异常。"""
        try:
            return listener_address(self.listen_host, self.port)
        except ValueError:
            host = self.listen_host.strip() if isinstance(self.listen_host, str) else ""
            return f"{host or '127.0.0.1'}:{self.port}"

    def command_string(self, binary: str = "cloudflared") -> str:
        try:
            argv = self.build_argv(binary)
        except ValueError:
            # 复制命令行时不应崩溃：extra_args 若合法仍按参数切分，仅在引号不配对时整段保留。
            hostname = self.hostname if isinstance(self.hostname, str) else str(self.hostname or "")
            argv = [
                binary, "access", "tcp",
                "--hostname", hostname,
                "--url", self.listen_label,
            ]
            if isinstance(self.extra_args, str) and self.extra_args.strip():
                try:
                    argv += shlex.split(self.extra_args)
                except ValueError:
                    argv.append(self.extra_args)
        return " ".join(shlex.quote(a) for a in argv)


class TunnelProcess(GObject.Object):
    """单个隧道对应的 cloudflared 子进程。"""

    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "log-line": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    MAX_LOG_LINE_BYTES = 64 * 1024
    READ_CHUNK_BYTES = 16 * 1024
    # cloudflared 先打印 “Start Websocket listener” 再去 bind 端口，端口被占用时紧接着
    # 报错退出；看到这行后进程还能再活这么久（毫秒），才算真的“已连接”。
    LISTENER_GRACE_MS = 400

    def __init__(self, config: TunnelConfig):
        super().__init__()
        self.config = config
        self.state = STATE_STOPPED
        self.last_error: Optional[str] = None
        self.started_at: Optional[float] = None
        self._log: deque[str] = deque(maxlen=3000)
        self._proc: Optional[Gio.Subprocess] = None
        self._stream: Optional[Gio.InputStream] = None
        self._read_cancel: Optional[Gio.Cancellable] = None
        self._line_buffer = bytearray()
        self._line_truncated = False
        self._listener_seen = False
        self._stopping = False
        # 普通 stop() 挂上的 5 秒强杀定时器；force 升级或进程退出时要摘掉
        self._kill_timer: Optional[int] = None
        self._promote_timer: Optional[int] = None
        self._exit_timer: Optional[int] = None
        # 进程已退出、但管道里可能还压着没读完的输出时，记下退出码，等 EOF 再收尾
        self._pending_exit: Optional[int] = None
        # 进程是被信号结束的（没有退出码）时记下信号编号，收尾时写进日志
        self._exit_signal: Optional[int] = None
        self._eof = False
        # 启动瞬间配置的快照：运行中编辑会替换 self.config（保存的新值），
        # 但实际监听的仍是旧端口；端口冲突检测必须用快照，否则会漏报/误报。
        self._running_snapshot: Optional[TunnelConfig] = None

    def running_config(self) -> TunnelConfig:
        """当前实际生效的配置：运行中用启动快照，否则用保存的配置。"""
        if self.is_active() and self._running_snapshot is not None:
            return self._running_snapshot
        return self.config

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
        try:
            argv = self.config.build_argv(binary)
        except ValueError as exc:
            # extra_args 引号不配对等情况：不要把异常抛给 GTK 信号分发（用户无感知），
            # 而是转为 ERROR 状态并写日志，UI 会弹出 toast + 日志按钮。
            self.last_error = f"隧道配置无效: {exc}"
            self._append(f"[启动失败] {self.last_error}")
            if self.state == STATE_ERROR:
                self.emit("state-changed", STATE_ERROR)
            else:
                self._set_state(STATE_ERROR)
            return
        self.last_error = None
        self.started_at = None
        self._stopping = False
        self._pending_exit = None
        self._eof = False
        self._listener_seen = False
        self._running_snapshot = self.config.copy()
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
            self._running_snapshot = None
            self._set_state(STATE_ERROR)
            return

        self._proc = proc
        self.started_at = time.monotonic()
        self._line_buffer.clear()
        self._line_truncated = False
        self._read_cancel = Gio.Cancellable()
        self._stream = proc.get_stdout_pipe()
        self._read_next(self._stream)
        proc.wait_async(None, self._on_exit)
        self._cancel_promote_timer()
        self._promote_timer = GLib.timeout_add(2500, self._promote_running, proc)

    def stop(self, force: bool = False) -> None:
        proc = self._proc
        # 子进程已退出、只是在等日志读完：没有可停的东西；此时置 _stopping 会把
        # 一次崩溃记成“用户主动断开”，错误状态和提示都会丢失。
        if proc is None or self._pending_exit is not None:
            return
        # 已经发过 SIGTERM、正在等它退出：再点一次“断开”不要重复发信号、重复记日志，
        # 也不要再挂一个 5 秒定时器；只有显式 force 才升级为强杀。
        if self._stopping and not force:
            return
        already_stopping = self._stopping
        self._stopping = True
        if not already_stopping:
            self._append("[正在断开…]")
        try:
            if force:
                proc.force_exit()
            else:
                proc.send_signal(signal.SIGTERM)
        except Exception:  # noqa: BLE001  (非 Unix 平台)
            proc.force_exit()
        self._cancel_kill_timer()
        if not force:
            self._kill_timer = GLib.timeout_add_seconds(5, self._force_kill, proc)

    def _cancel_kill_timer(self) -> None:
        if self._kill_timer is not None:
            GLib.source_remove(self._kill_timer)
            self._kill_timer = None

    def _cancel_promote_timer(self) -> None:
        if self._promote_timer is not None:
            GLib.source_remove(self._promote_timer)
            self._promote_timer = None

    def _cancel_exit_timer(self) -> None:
        if self._exit_timer is not None:
            GLib.source_remove(self._exit_timer)
            self._exit_timer = None

    # ------------------------------------------------------------ 回调
    def _force_kill(self, proc: Gio.Subprocess) -> bool:
        self._kill_timer = None
        if self._proc is proc and self._pending_exit is None:
            self._append("[进程未响应，强制结束]")
            proc.force_exit()
        return False

    def _promote_running(self, proc: Gio.Subprocess) -> bool:
        self._promote_timer = None
        # 用户在这 2.5 秒里点了断开时不要再报“已连接”，否则会弹出一条反直觉的提示
        if (self._proc is proc and self.state == STATE_STARTING
                and not self._stopping and self._pending_exit is None):
            self._set_state(STATE_RUNNING)
        return False

    def _read_next(self, stream: Gio.InputStream) -> None:
        stream.read_bytes_async(
            self.READ_CHUNK_BYTES, GLib.PRIORITY_DEFAULT,
            self._read_cancel, self._on_read, stream,
        )

    def _flush_log_line(self) -> None:
        text = self._line_buffer.decode("utf-8", errors="replace").rstrip("\r")
        if self._line_truncated:
            text += " … [日志行过长，已截断]"
        self._line_buffer.clear()
        self._line_truncated = False
        if not text:
            return
        self._append(text)
        low = text.lower()
        if (
            not self._listener_seen and self.state == STATE_STARTING
            and not self._stopping and not self._eof and self._pending_exit is None
            and ("start websocket listener" in low or "listening on" in low)
        ):
            # 这行日志出现在 bind 之前：此刻就置为“已连接”的话，端口被占用时界面会先弹
            # “已连接”、紧接着又弹“连接失败”。进程在宽限期内退出时 _on_exit 会摘掉定时器。
            self._listener_seen = True
            self._cancel_promote_timer()
            self._promote_timer = GLib.timeout_add(
                self.LISTENER_GRACE_MS, self._promote_running, self._proc
            )
        if (
            " err " in f" {low} "
            or " ftl " in f" {low} "
            or "level=error" in low
            or "level=fatal" in low
            or low.lstrip().startswith(("error", "fatal", "incorrect usage"))
        ):
            self.last_error = text

    def _on_read(self, stream: Gio.InputStream, result, _data) -> None:
        # DataInputStream.read_line_finish 在部分 PyGObject 版本中把 EOF 的 NULL
        # 和空行都映射成 b''，无法靠 raw is None 判断，会一直重读 EOF 直到兜底超时。
        # read_bytes_finish 返回 GBytes，零长度只表示 EOF；同时避免无换行日志无限分配。
        try:
            data = stream.read_bytes_finish(result).get_data()
        except GLib.Error:
            data = b""
        if stream is not self._stream:
            return
        if not data:
            self._eof = True
            self._flush_log_line()  # 保留最后一行，即使它没有换行符
            if self._pending_exit is not None:
                self._finish_exit()
            return
        parts = data.split(b"\n")
        for index, part in enumerate(parts):
            room = self.MAX_LOG_LINE_BYTES - len(self._line_buffer)
            self._line_buffer.extend(part[:room])
            if len(part) > room:
                self._line_truncated = True
            if index < len(parts) - 1:
                self._flush_log_line()
        self._read_next(stream)

    def _on_exit(self, proc: Gio.Subprocess, result) -> None:
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if proc is not self._proc:
            return
        if proc.get_if_exited():
            code, self._exit_signal = proc.get_exit_status(), None
        else:
            # 断开时的 SIGTERM、强杀、OOM 等：没有退出码可言
            code = -1
            self._exit_signal = proc.get_term_sig() if proc.get_if_signaled() else None
        self.started_at = None
        self._cancel_kill_timer()
        self._cancel_promote_timer()
        if self._stream is not None and not self._eof:
            # 管道里可能还压着没读完的输出，等 _on_read 读到 EOF 再改状态；
            # 万一写入端被子进程的后代长期持有，兜底定时器负责收尾。
            self._pending_exit = code
            self._cancel_exit_timer()
            self._exit_timer = GLib.timeout_add(2000, self._finish_exit_fallback, proc)
        else:
            self._finish_exit(code)

    def _finish_exit_fallback(self, proc: Gio.Subprocess) -> bool:
        """兜底定时器：只对“它自己那一次”进程生效。

        不带 proc 直接挂 ``_finish_exit`` 的话，用户在 2 秒内断开又重连、新进程也很快
        退出时，旧定时器会把新进程的 ``_pending_exit`` 提前消费掉，新进程最后几行日志丢失。
        """
        self._exit_timer = None
        if self._proc is proc and self._pending_exit is not None:
            self._finish_exit()
        return False

    def _finish_exit(self, code: Optional[int] = None) -> bool:
        """进程退出后的统一收尾：EOF 回调与兜底定时器谁先到谁负责，只生效一次。"""
        if code is None:
            if self._pending_exit is None:
                return False
            code, self._pending_exit = self._pending_exit, None
        else:
            self._pending_exit = None
        self._cancel_kill_timer()
        self._cancel_promote_timer()
        self._cancel_exit_timer()
        # 兜底收尾时管道可能由后代进程持有；取消未完成的读取，释放回调引用。
        if self._read_cancel is not None:
            self._read_cancel.cancel()
            self._read_cancel = None
        self._eof = True
        self._flush_log_line()
        self._proc = None
        self._stream = None
        self._running_snapshot = None
        sig, self._exit_signal = self._exit_signal, None
        if sig is None:
            self._append(f"[进程已退出，状态码 {code}]")
        else:
            self._append(f"[进程已被信号 {_signal_name(sig)} 终止]")
        stopped = self._stopping or code == 0
        self._stopping = False
        self._set_state(STATE_STOPPED if stopped else STATE_ERROR)
        return False


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
        # 从 UI 删除不等于子进程已经退出；退出前仍需跟踪以便冲突检测和 shutdown 强杀。
        self._retiring: set[TunnelProcess] = set()
        raw_tunnels = config.get("tunnels", []) or []
        # 手改 config.json 可能把 tunnels 写成 dict/字符串/null：非 list 直接忽略，
        # 否则遍历 dict 的 key（字符串）会让 from_dict 崩溃、应用无窗口。
        if not isinstance(raw_tunnels, list):
            raw_tunnels = []
        for item in raw_tunnels:
            if not isinstance(item, dict):
                continue
            self._wrap(TunnelConfig.from_dict(item))

    def _wrap(self, cfg: TunnelConfig) -> TunnelProcess:
        self._ensure_unique_id(cfg)
        proc = TunnelProcess(cfg)
        proc.connect("state-changed", self._on_process_state)
        self.procs.append(proc)
        return proc

    def _on_process_state(self, proc: TunnelProcess, state: str) -> None:
        if proc in self._retiring:
            if not proc.is_active():
                self._retiring.discard(proc)
            return
        if proc in self.procs:
            self.emit("state-changed", proc.config.id, state)

    def _ensure_unique_id(self, cfg: TunnelConfig) -> TunnelConfig:
        """保证 id 是非空且唯一的字符串。

        手工编辑（或从别处拷贝）config.json 时很容易出现 id 缺失 / 为 null / 两条重复，
        那会让 get() 永远只命中第一条——在 UI 上删除/编辑其中一行，实际操作的是另一条。
        """
        self._retiring = {p for p in self._retiring if p.is_active()}
        taken = {p.config.id for p in [*self.procs, *self._retiring]}
        if not isinstance(cfg.id, str) or not cfg.id.strip() or cfg.id in taken:
            cfg.id = uuid.uuid4().hex[:10]
        return cfg

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
        self.procs.remove(proc)
        if proc.is_active():
            self._retiring.add(proc)
        proc.stop()
        if not proc.is_active():
            self._retiring.discard(proc)
        self.save()
        self.emit("tunnels-changed")

    def _next_free_port(self, port: int) -> int:
        """从 ``port`` 之后开始找下一个没有任何隧道声明的端口（含正在退出的）。

        原端口 >= 1024 时只在 1024–65535 内绕回：从 65535 绕回 1 会得到需要特权才能
        监听的端口，副本一启动就会因权限不足失败。
        """
        port = int(port)
        used = {p.config.port for p in self.procs}
        used.update(p.running_config().port for p in self.procs if p.is_active())
        used.update(p.config.port for p in self._retiring)
        used.update(p.running_config().port for p in self._retiring if p.is_active())
        low = 1024 if port >= 1024 else 1
        span = 65535 - low + 1
        for offset in range(1, span + 1):
            candidate = (port - low + offset) % span + low
            if candidate not in used:
                return candidate
        return port  # pragma: no cover  (范围内的端口全被占用)

    def duplicate(self, tunnel_id: str) -> Optional[TunnelProcess]:
        proc = self.get(tunnel_id)
        if proc is None:
            return None
        cfg = proc.config.copy()
        cfg.id = uuid.uuid4().hex[:10]
        cfg.name = f"{proc.config.display_name} (副本)"
        cfg.autostart = False
        # 原隧道正在运行时，副本沿用同一个 host:port 根本连不上；仅在这种真冲突下
        # 顺延到空闲端口，未运行时保持用户原来的端口不变。
        if self.port_conflict(cfg) is not None:
            cfg.port = self._next_free_port(cfg.port)
        return self.add(cfg)

    # ------------------------------------------------------------ 批量
    def active(self) -> list[TunnelProcess]:
        return [p for p in self.procs if p.is_active()]

    def port_conflict(self, cfg: TunnelConfig) -> Optional[TunnelProcess]:
        def _norm_host(host: object) -> str:
            h = host if isinstance(host, str) else ""
            h = (h or "127.0.0.1").strip().lower() or "127.0.0.1"
            if h.startswith("[") and h.endswith("]"):
                h = h[1:-1]
            try:
                addr = ipaddress.ip_address(h)
                if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
                    addr = addr.ipv4_mapped
                h = str(addr)
            except ValueError:
                h = h.rstrip(".") or h
            # 保守地把 localhost / ::1 与 IPv4 回环视为重叠，避免双栈监听漏报
            if h in ("localhost", "::1"):
                h = "127.0.0.1"
            return h

        self._retiring = {p for p in self._retiring if p.is_active()}
        want_host, want_port = _norm_host(cfg.listen_host), cfg.port
        for p in [*self.active(), *self._retiring]:
            if p not in self._retiring and p.config.id == cfg.id:
                continue
            # 用启动快照而非当前保存值：运行中编辑改了端口后，实际占用的仍是旧端口
            running = p.running_config()
            if running.port != want_port:
                continue
            have_host = _norm_host(running.listen_host)
            # 0.0.0.0 / :: 绑定所有地址，与任何具体地址都冲突
            any_addrs = ("0.0.0.0", "::", "0:0:0:0:0:0:0:0")
            if have_host in any_addrs or want_host in any_addrs or have_host == want_host:
                return p
        return None

    def stop_all(self, force: bool = False) -> None:
        for p in [*self.procs, *self._retiring]:
            p.stop(force=force)
