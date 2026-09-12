"""对话框：隧道编辑、实时日志窗口"""
from __future__ import annotations

import shlex
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from .tunnel import STATE_LABELS, TunnelConfig, TunnelProcess  # noqa: E402


def _escape_shortcut(window: Gtk.Window) -> None:
    """让 Esc 键关闭窗口。"""
    controller = Gtk.ShortcutController()
    controller.set_scope(Gtk.ShortcutScope.LOCAL)
    trigger = Gtk.ShortcutTrigger.parse_string("Escape")
    action = Gtk.CallbackAction.new(lambda *_: (window.close(), True)[1])
    controller.add_shortcut(Gtk.Shortcut.new(trigger, action))
    window.add_controller(controller)


def copy_text(widget: Gtk.Widget, text: str) -> None:
    provider = Gdk.ContentProvider.new_for_bytes(
        "text/plain;charset=utf-8", GLib.Bytes.new(text.encode("utf-8"))
    )
    widget.get_clipboard().set_content(provider)


class TunnelDialog(Adw.Window):
    """新建 / 编辑隧道。"""

    def __init__(
        self,
        parent: Gtk.Window,
        config: Optional[TunnelConfig],
        on_save: Callable[[TunnelConfig], None],
    ):
        super().__init__(transient_for=parent, modal=True, default_width=540, default_height=560)
        self.is_new = config is None
        self.cfg = config or TunnelConfig()
        self.on_save = on_save
        self.set_title("新建隧道" if self.is_new else "编辑隧道")
        _escape_shortcut(self)

        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="取消")
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="保存", css_classes=["suggested-action"])
        save.connect("clicked", self._on_save_clicked)
        header.pack_start(cancel)
        header.pack_end(save)
        view.add_top_bar(header)

        self.toasts = Adw.ToastOverlay()
        view.set_content(self.toasts)
        page = Adw.PreferencesPage()
        self.toasts.set_child(page)

        # ---------- 基本信息
        g_basic = Adw.PreferencesGroup(title="基本信息")
        self.name_row = Adw.EntryRow(title="名称（可选）")
        self.name_row.set_text(self.cfg.name)
        self.host_row = Adw.EntryRow(title="隧道主机名 (hostname)")
        self.host_row.set_text(self.cfg.hostname)
        for row in (self.name_row, self.host_row):
            g_basic.add(row)
        page.add(g_basic)

        # ---------- 本地监听
        g_local = Adw.PreferencesGroup(
            title="本地监听", description="cloudflared 会在本机监听该地址，并将流量转发至隧道另一端。"
        )
        self.port_row = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.port_row.set_title("本地端口")
        self.port_row.set_value(self.cfg.port)
        self.port_row.connect("notify::value", lambda *_: self._update_port_hint())
        self.listen_row = Adw.EntryRow(title="监听地址")
        self.listen_row.set_text(self.cfg.listen_host or "127.0.0.1")
        self.port_hint = Adw.ActionRow(
            title="端口低于 1024 属于周知端口，可能需要管理员权限 (sudo)",
            css_classes=["warning"],
        )
        self.port_hint.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        for row in (self.port_row, self.listen_row, self.port_hint):
            g_local.add(row)
        page.add(g_local)
        self._update_port_hint()

        # ---------- 高级
        g_adv = Adw.PreferencesGroup(title="高级")
        self.autostart_row = Adw.SwitchRow(title="程序启动时自动连接")
        self.autostart_row.set_active(self.cfg.autostart)
        self.extra_row = Adw.EntryRow(title="额外命令行参数（可选）")
        self.extra_row.set_text(self.cfg.extra_args)
        g_adv.add(self.autostart_row)
        g_adv.add(self.extra_row)
        page.add(g_adv)

        self.set_content(view)
        self.host_row.grab_focus()

    def _update_port_hint(self) -> None:
        self.port_hint.set_visible(int(self.port_row.get_value()) < 1024)

    def _toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(message))

    def _on_save_clicked(self, *_args) -> None:
        hostname = self.host_row.get_text().strip()
        if not hostname or " " in hostname:
            self._toast("请输入有效的隧道主机名")
            self.host_row.grab_focus()
            return
        port = int(self.port_row.get_value())
        if not 1 <= port <= 65535:
            self._toast("端口必须在 1 到 65535 之间")
            return
        extra_args = self.extra_row.get_text().strip()
        if extra_args:
            # 引号不配对时 shlex.split 会抛 ValueError：这里提前拦截，
            # 否则保存后点“连接”会直接失败（之前是静默无反应）。
            try:
                shlex.split(extra_args)
            except ValueError as exc:
                self._toast(f"额外参数格式错误: {exc}")
                self.extra_row.grab_focus()
                return
        cfg = self.cfg
        cfg.name = self.name_row.get_text().strip()
        cfg.hostname = hostname
        cfg.port = port
        cfg.listen_host = self.listen_row.get_text().strip() or "127.0.0.1"
        cfg.autostart = self.autostart_row.get_active()
        cfg.extra_args = extra_args
        self.on_save(cfg)
        self.close()


class LogWindow(Adw.Window):
    """某个隧道的实时输出。"""

    def __init__(self, parent: Gtk.Window, proc: TunnelProcess):
        super().__init__(transient_for=parent, default_width=760, default_height=480)
        self.proc = proc
        self.set_title(f"日志 · {proc.config.display_name}")
        _escape_shortcut(self)

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title=proc.config.display_name, subtitle="")
        header.set_title_widget(self.title_widget)

        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="复制全部")
        copy_btn.connect("clicked", self._copy_all)
        clear_btn = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text="清空日志")
        clear_btn.connect("clicked", self._clear)
        self.follow_btn = Gtk.ToggleButton(
            icon_name="go-bottom-symbolic", tooltip_text="自动滚动到底部", active=True
        )
        header.pack_end(copy_btn)
        header.pack_end(clear_btn)
        header.pack_end(self.follow_btn)
        view.add_top_bar(header)

        self.buffer = Gtk.TextBuffer()
        self.textview = Gtk.TextView(
            buffer=self.buffer,
            editable=False,
            cursor_visible=False,
            monospace=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            css_classes=["log-view"],
        )
        self.end_mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        scroller = Gtk.ScrolledWindow(child=self.textview, vexpand=True)
        view.set_content(scroller)
        self.set_content(view)

        for line in proc.log_lines():
            self._append(line)
        self._update_subtitle()
        self._hid_log = proc.connect("log-line", lambda _p, line: self._append(line))
        self._hid_state = proc.connect("state-changed", lambda *_: self._update_subtitle())
        self.connect("close-request", self._on_close)

    def _update_subtitle(self) -> None:
        self.title_widget.set_subtitle(STATE_LABELS.get(self.proc.state, self.proc.state))

    def _append(self, line: str) -> None:
        end = self.buffer.get_end_iter()
        self.buffer.insert(end, line + "\n")
        # TextBuffer 不像 deque 有 maxlen：cloudflared 开 debug 日志时会狂刷输出，
        # 不截断的话窗口开久了内存暴涨、UI 卡死。与 TunnelProcess 保持同样的 3000 行上限。
        if self.buffer.get_line_count() > 3200:
            start = self.buffer.get_start_iter()
            cut = self.buffer.get_iter_at_line(200)
            self.buffer.delete(start, cut)
        if self.follow_btn.get_active():
            self.textview.scroll_to_mark(self.end_mark, 0.0, False, 0.0, 1.0)

    def _copy_all(self, *_args) -> None:
        start, end = self.buffer.get_bounds()
        copy_text(self, self.buffer.get_text(start, end, False))

    def _clear(self, *_args) -> None:
        self.proc.clear_log()
        self.buffer.set_text("")

    def _on_close(self, *_args) -> bool:
        self.proc.disconnect(self._hid_log)
        self.proc.disconnect(self._hid_state)
        return False
