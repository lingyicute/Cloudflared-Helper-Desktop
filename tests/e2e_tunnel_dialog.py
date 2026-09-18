#!/usr/bin/env python3
"""新建 / 编辑隧道对话框的端到端测试（不含剪贴板那条路径）。

``TunnelDialog`` 被剪贴板快速连接和普通新建共用，这里守住普通路径不被改坏：
标题、初始焦点、鼠标点“保存”、以及编辑已有隧道时的行为。

    xvfb-run -a python3 tests/e2e_tunnel_dialog.py

步骤驱动约定与 e2e_quick_tunnel.py 相同：步骤函数在条件尚未就绪时返回一个零参
谓词，驱动每 50 ms 重跑该步骤，直到谓词为真或超过 4 s；否则返回 None 立即进入下一步。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cftm-dlg-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMP, "data")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from cftm.app import Application  # noqa: E402
from cftm.dialogs import TunnelDialog, _find_spin_button  # noqa: E402

#: 条件轮询间隔 / 单步等待上限
POLL_INTERVAL_MS = 50
WAIT_TIMEOUT_MS = 4000

FAILURES = []
PASSED = []


def check(cond, label, extra=""):
    (PASSED if cond else FAILURES).append(label)
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{extra}]" if extra else ""))


def editors():
    return [w for w in Gtk.Window.list_toplevels() if isinstance(w, TunnelDialog) and w.get_mapped()]


def xdotool(*args):
    import subprocess
    subprocess.run(["xdotool", *args], capture_output=True, text=True)


def is_inside(widget, ancestor):
    node = widget
    while node is not None:
        if node is ancestor:
            return True
        node = node.get_parent()
    return False


def find_button(widget, label):
    stack = [widget]
    while stack:
        node = stack.pop()
        if isinstance(node, Gtk.Button) and node.get_label() == label:
            return node
        child = node.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return None


class TestApp(Application):
    def do_activate(self):
        Application.do_activate(self)
        self.win = self.props.active_window
        self.steps = [
            (500, self.s1_new), (100, self.s2_check_new), (300, self.s3_fill_and_save),
            (100, self.s4_check_saved), (200, self.s5_edit), (100, self.s6_check_edit),
            (300, self.s7_port_enter), (100, self.s8_finish),
        ]
        self.run_steps()

    def run_steps(self):
        if not self.steps:
            self.quit()
            return
        delay, fn = self.steps[0]
        deadline = [0]

        def tick():
            try:
                result = fn()
            except Exception as exc:
                FAILURES.append(f"{fn.__name__} raised {exc!r}")
                print(f"  FAIL  {fn.__name__} raised {exc!r}")
                result = None
            if callable(result):
                now = GLib.get_monotonic_time()
                if deadline[0] == 0:
                    deadline[0] = now + WAIT_TIMEOUT_MS * 1000
                if now >= deadline[0]:
                    FAILURES.append(f"{fn.__name__}: 等待条件超时（{WAIT_TIMEOUT_MS} ms）")
                    print(f"  FAIL  {fn.__name__}: 等待条件超时（{WAIT_TIMEOUT_MS} ms）")
                    self.steps.pop(0)
                    self.run_steps()
                else:
                    GLib.timeout_add(POLL_INTERVAL_MS, tick)
                return False
            self.steps.pop(0)
            self.run_steps()
            return False

        GLib.timeout_add(delay, tick)

    def s1_new(self):
        print("\n[1] Ctrl+N 走 win.new-tunnel 打开新建对话框")
        self.win.lookup_action("new-tunnel").activate(None)

    def s2_check_new(self):
        d = editors()
        if len(d) != 1:
            return lambda: len(editors()) == 1
        print("[1-断言] 新建对话框内容正确")
        self.dlg = d[0]
        check(self.dlg.get_title() == "新建隧道", "标题是“新建隧道”", self.dlg.get_title())
        check(self.dlg.is_new is True, "is_new 仍按 config is None 推断为 True")
        check(is_inside(self.dlg.get_focus(), self.dlg.host_row), "焦点默认落在主机名输入框",
              type(self.dlg.get_focus()).__name__)
        check(self.dlg.name_row.get_text() == "", "名称为空")
        check(self.dlg.host_row.get_text() == "", "主机名为空")
        check(int(self.dlg.port_row.get_value()) == 21128, "端口是默认值", str(self.dlg.port_row.get_value()))
        # 回车保存不该影响端口行的可用范围
        spin = _find_spin_button(self.dlg.port_row)
        check(spin is not None, "端口行内部能找到 Gtk.SpinButton")

    def s3_fill_and_save(self):
        print("[2] 填好字段，用鼠标点“保存”")
        self.dlg.name_row.set_text("家里的 NAS")
        self.dlg.host_row.set_text("nas.example.com")
        self.dlg.port_row.set_value(445)
        self.dlg.listen_row.set_text("127.0.0.1")
        find_button(self.dlg, "保存").emit("clicked")

    def s4_check_saved(self):
        if editors():
            return lambda: not editors()
        check(not editors(), "点“保存”后对话框关闭")
        check(len(self.manager.procs) == 1, "新增 1 条隧道", f"got {len(self.manager.procs)}")
        if self.manager.procs:
            cfg = self.manager.procs[0].config
            check((cfg.name, cfg.hostname, cfg.port) == ("家里的 NAS", "nas.example.com", 445),
                  "保存的字段正确", f"{cfg.name}/{cfg.hostname}/{cfg.port}")
        cfg_path = Path(os.environ["XDG_CONFIG_HOME"]) / "cloudflared-tunnel-manager" / "config.json"
        check(cfg_path.exists(), "config.json 已写入", str(cfg_path))

    def s5_edit(self):
        print("[3] 编辑已有隧道")
        self.win.lookup_action("tunnel-edit").activate(
            GLib.Variant.new_string(self.manager.procs[0].config.id))

    def s6_check_edit(self):
        d = editors()
        if len(d) != 1:
            return lambda: len(editors()) == 1
        print("[3-断言] 编辑对话框回填正确")
        self.dlg = d[0]
        check(self.dlg.get_title() == "编辑隧道", "标题是“编辑隧道”", self.dlg.get_title())
        check(self.dlg.is_new is False, "is_new 为 False")
        check(self.dlg.host_row.get_text() == "nas.example.com", "字段回填正确", self.dlg.host_row.get_text())
        check(is_inside(self.dlg.get_focus(), self.dlg.host_row), "焦点仍落在主机名输入框",
              type(self.dlg.get_focus()).__name__)

    def s7_port_enter(self):
        print("[4] 编辑态下改端口后回车保存")
        self.dlg.port_row.set_value(4455)
        self.dlg.port_row.grab_focus()
        check(is_inside(self.dlg.get_focus(), self.dlg.port_row), "焦点能移到端口输入框",
              type(self.dlg.get_focus()).__name__)
        # 窗口关闭是异步的（unmap 要等下一轮），s8 用轮询等它真正消失
        GLib.timeout_add(250, lambda: (xdotool("key", "Return"), False)[1])

    def s8_finish(self):
        if editors():
            return lambda: not editors()
        check(not editors(), "回车后对话框关闭")
        cfg = self.manager.procs[0].config
        check(cfg.port == 4455, "端口已更新", str(cfg.port))
        check(len(self.manager.procs) == 1, "没有多出一条隧道", f"got {len(self.manager.procs)}")
        print(f"\n===== {len(PASSED)} passed, {len(FAILURES)} failed =====")
        for f in FAILURES:
            print("FAILED:", f)
        self.quit()


GLib.timeout_add(60000, lambda: (print("WATCHDOG TIMEOUT"), os._exit(2))[1])
TestApp().run([])
sys.exit(1 if FAILURES else 0)
