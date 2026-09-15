#!/usr/bin/env python3
"""剪贴板快速隧道的端到端测试。

真实的焦点切换 + 真实的 X 剪贴板 + 真实的按钮点击，被测对象就是
``cftm.app.Application`` 本身（TestApp 只是把测试步骤挂在 do_activate 后面）。

需要一个 X 显示和两个小工具（Wayland 下请改用 XWayland 会话）：

    sudo apt install xvfb xdotool xclip
    xvfb-run -a python3 tests/e2e_quick_tunnel.py

覆盖的场景：焦点切回窗口后弹窗、点“是”后唤出预填好的新建对话框、敲端口号回车即保存、
同一条链接不重复询问、新链接重新询问、菜单开关关闭后不再弹窗、普通文本不弹窗。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# 配置和数据都写到临时目录，绝不碰用户真实的 ~/.config
_TMP = tempfile.mkdtemp(prefix="cftm-e2e-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMP, "data")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from cftm.app import Application  # noqa: E402
from cftm.dialogs import TunnelDialog  # noqa: E402

URL = "https://e2e-demo-abc.trycloudflare.com/"
HOST = "e2e-demo-abc.trycloudflare.com"
URL2 = "https://second-link-xyz.trycloudflare.com"
URL3 = "https://third-link-qrs.trycloudflare.com"
URL4 = "https://fourth-link-tuv.trycloudflare.com"
URL5 = "https://fifth-link-wxy.trycloudflare.com"

FAILURES = []
PASSED = []


def check(cond, label, extra=""):
    (PASSED if cond else FAILURES).append(label)
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{extra}]" if extra else ""))


def set_clipboard(text):
    subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=True)


def xdotool(*args):
    subprocess.run(["xdotool", *args], capture_output=True, text=True)


def walk(widget):
    stack = [widget]
    while stack:
        node = stack.pop()
        yield node
        child = node.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()


def find_button(widget, label):
    return next((w for w in walk(widget)
                 if isinstance(w, Gtk.Button) and w.get_label() == label), None)


def message_dialogs():
    """只数还开着的（mapped）确认框：关掉的窗口在销毁前仍会留在 list_toplevels() 里。"""
    return [w for w in Gtk.Window.list_toplevels()
            if isinstance(w, Adw.MessageDialog) and w.get_mapped()]


def is_inside(widget, ancestor):
    node = widget
    while node is not None:
        if node is ancestor:
            return True
        node = node.get_parent()
    return False


def label_text(widget):
    return " | ".join(w.get_label() or "" for w in walk(widget) if isinstance(w, Gtk.Label))


class TestApp(Application):
    def do_activate(self):
        Application.do_activate(self)
        self.win = self.props.active_window
        self.win.set_title("cftm-main")
        # 另一个窗口，用来把焦点从主窗口上抢走
        self.decoy = Adw.ApplicationWindow(application=self, title="decoy",
                                           default_width=200, default_height=120)
        self.decoy.present()
        self.steps = [
            (700, self.s1_arm), (400, self.s2_focus_main), (900, self.s3_expect_dialog),
            (200, self.s4_click_yes), (600, self.s5_expect_editor), (400, self.s6_type_and_enter),
            (700, self.s7_expect_saved), (200, self.s8_refocus_dedup), (1000, self.s9_expect_no_dialog),
            (200, self.s10_new_url), (1000, self.s11_expect_dialog2), (200, self.s12_disable),
            (1000, self.s13_expect_no_dialog3), (200, self.s14_non_matching),
            (1000, self.s15_second_while_open), (600, self.s16_close_and_retry),
            (600, self.s17_finish),
        ]
        self.run_steps()

    # ------------------------------------------------------------ 步骤驱动
    def run_steps(self):
        if not self.steps:
            self.quit()
            return
        delay, fn = self.steps.pop(0)

        def tick():
            try:
                fn()
            except Exception as exc:  # 单步炸了也要把后面的检查跑完
                FAILURES.append(f"{fn.__name__} raised {exc!r}")
                print(f"  FAIL  {fn.__name__} raised {exc!r}")
            self.run_steps()
            return False

        GLib.timeout_add(delay, tick)

    def focus(self, window):
        """用 xdotool 真正改变 X 焦点（等价于用户切窗口）。"""
        out = subprocess.run(["xdotool", "search", "--name", window.get_title()],
                             capture_output=True, text=True).stdout.strip().splitlines()
        if out:
            xdotool("windowfocus", out[0])

    # ------------------------------------------------------------ 场景
    def s1_arm(self):
        print("\n[1] 剪贴板放入快速隧道链接，焦点在别的窗口上")
        set_clipboard(URL)
        self.focus(self.decoy)

    def s2_focus_main(self):
        print("[2] 焦点切回主窗口（触发 EventControllerFocus enter）")
        self.focus(self.win)

    def s3_expect_dialog(self):
        print("[3] 期望弹出确认框")
        dlgs = message_dialogs()
        check(len(dlgs) == 1, "恰好弹出 1 个确认框", f"got {len(dlgs)}")
        if not dlgs:
            return
        self.confirm = dlgs[0]
        check(self.confirm.get_heading() == "是否要连接这个快速隧道？",
              "标题为“是否要连接这个快速隧道？”", self.confirm.get_heading())
        check(f"https://{HOST}" in label_text(self.confirm), "确认框里展示了该链接", label_text(self.confirm)[:90])
        check(find_button(self.confirm, "是") is not None and find_button(self.confirm, "否") is not None,
              "有“是 / 否”两个按钮")
        check(self.confirm.get_modal(), "确认框是模态的")

    def s4_click_yes(self):
        print("[4] 点击“是”")
        find_button(self.confirm, "是").emit("clicked")

    def s5_expect_editor(self):
        print("[5] 期望唤出新建隧道对话框")
        editors = [w for w in Gtk.Window.list_toplevels() if isinstance(w, TunnelDialog)]
        check(len(editors) == 1, "唤出 1 个新建隧道对话框", f"got {len(editors)}")
        if not editors:
            return
        self.editor = editors[0]
        check(self.editor.get_title() == "新建隧道", "标题是“新建隧道”", self.editor.get_title())
        check(self.editor.host_row.get_text() == HOST, "主机名已填好", self.editor.host_row.get_text())
        name = self.editor.name_row.get_text()
        check(re.fullmatch(r"\d{8} \d{2}:\d{2}:\d{2} 快速隧道", name) is not None,
              "名称为 yyyymmdd hh:mm:ss 快速隧道", name)
        focus = self.editor.get_focus()
        # GTK 4.10+ 的 SpinButton 由内部 Gtk.Text 组合而成，焦点落在那个 Gtk.Text 上
        check(focus is not None and is_inside(focus, self.editor.port_row),
              "焦点落在端口输入框上", type(focus).__name__)
        sel = self.editor.port_row.get_selection_bounds()
        text = self.editor.port_row.get_text()
        check(tuple(sel) == (0, len(text)), "端口默认值被全选（直接输入即覆盖）",
              f"selection={sel} text={text!r}")

    def s6_type_and_enter(self):
        print("[6] 像用户一样敲端口号，然后按回车")
        xdotool("type", "--delay", "30", "22222")
        GLib.timeout_add(300, lambda: (xdotool("key", "Return"), False)[1])

    def s7_expect_saved(self):
        print("[7] 期望隧道已保存")
        editors = [w for w in Gtk.Window.list_toplevels() if isinstance(w, TunnelDialog)]
        check(not editors, "回车后对话框已关闭（回车 = 保存）", f"still open: {len(editors)}")
        procs = self.manager.procs
        check(len(procs) == 1, "管理器里新增 1 条隧道", f"got {len(procs)}")
        if procs:
            cfg = procs[0].config
            check(cfg.hostname == HOST, "保存的 hostname 正确", cfg.hostname)
            check(cfg.port == 22222, "保存的端口是刚敲进去的 22222", str(cfg.port))
            check(cfg.name.endswith("快速隧道"), "保存的名称正确", cfg.name)
            check(cfg.build_argv("cloudflared")[:7] ==
                  ["cloudflared", "access", "tcp", "--hostname", HOST, "--url", "127.0.0.1:22222"],
                  "生成的命令行正确", " ".join(cfg.build_argv("cloudflared")))
        path = os.path.join(os.environ["XDG_CONFIG_HOME"], "cloudflared-tunnel-manager", "config.json")
        check(os.path.exists(path), "config.json 已创建", path)
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        check(len(saved.get("tunnels", [])) == 1, "已落盘到 config.json",
              json.dumps(saved.get("tunnels"), ensure_ascii=False)[:140])
        check(saved.get("clipboard_quick_tunnel") is not False, "开关状态默认为开", str(saved.get("clipboard_quick_tunnel")))

    def s8_refocus_dedup(self):
        print("[8] 剪贴板不变，再切出去/切回来 —— 不应重复弹窗")
        self.focus(self.decoy)
        GLib.timeout_add(300, lambda: (self.focus(self.win), False)[1])

    def s9_expect_no_dialog(self):
        dlgs = message_dialogs()
        check(not dlgs, "同一条链接不重复询问", f"got {len(dlgs)}")

    def s10_new_url(self):
        print("[9] 换成另一条链接 —— 应当重新询问")
        set_clipboard(URL2)
        self.focus(self.decoy)
        GLib.timeout_add(300, lambda: (self.focus(self.win), False)[1])

    def s11_expect_dialog2(self):
        dlgs = message_dialogs()
        check(len(dlgs) == 1, "新链接重新弹出确认框", f"got {len(dlgs)}")
        for d in dlgs:
            check("second-link-xyz.trycloudflare.com" in label_text(d), "展示的是新链接")
            find_button(d, "否").emit("clicked")

    def s12_disable(self):
        print("[10] 关掉功能开关后再次切换焦点 —— 不应弹窗")
        action = self.win.lookup_action("clipboard-quick-tunnel")
        check(action is not None, "菜单里存在“剪贴板快速新建”开关")
        if action:
            action.change_state(GLib.Variant.new_boolean(False))
            check(self.config.get("clipboard_quick_tunnel") is False, "关闭状态已写入配置")
        set_clipboard(URL3)
        self.focus(self.decoy)
        GLib.timeout_add(300, lambda: (self.focus(self.win), False)[1])

    def s13_expect_no_dialog3(self):
        dlgs = message_dialogs()
        check(not dlgs, "关闭开关后不再弹窗", f"got {len(dlgs)}")

    def s14_non_matching(self):
        print("[11] 重新开启开关，但剪贴板是普通文本 —— 不应弹窗")
        self.win.lookup_action("clipboard-quick-tunnel").change_state(GLib.Variant.new_boolean(True))
        set_clipboard("hello world，这不是链接")
        self.focus(self.decoy)
        GLib.timeout_add(300, lambda: (self.focus(self.win), False)[1])

    def s15_second_while_open(self):
        print("[12] 确认框还开着时来了第二条链接 —— 不能被吞掉")
        watcher = self.win.quick_tunnel
        first = watcher.consider(URL4)
        check(first == "fourth-link-tuv.trycloudflare.com", "第一条链接正常弹出", str(first))
        second = watcher.consider(URL5)
        check(second is None, "确认框开着时不消耗第二条链接", str(second))
        for d in message_dialogs():
            find_button(d, "否").emit("clicked")

    def s16_close_and_retry(self):
        print("[13] 关掉确认框后，第二条链接仍然要能弹出来")
        watcher = self.win.quick_tunnel
        again = watcher.consider(URL5)
        check(again == "fifth-link-wxy.trycloudflare.com", "第二条链接这次被问到", str(again))
        dlgs = message_dialogs()
        check(len(dlgs) == 1 and "fifth-link-wxy" in label_text(dlgs[0]),
              "弹出的是第二条链接的确认框", f"got {len(dlgs)}")
        for d in dlgs:
            find_button(d, "否").emit("clicked")

    def s17_finish(self):
        print(f"\n===== {len(PASSED)} passed, {len(FAILURES)} failed =====")
        for f in FAILURES:
            print("FAILED:", f)
        self.quit()


GLib.timeout_add(90000, lambda: (print("WATCHDOG TIMEOUT"), os._exit(2))[1])
TestApp().run([])
sys.exit(1 if FAILURES else 0)
