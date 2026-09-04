"""“版本管理”页面：当前二进制、已安装版本、GitHub 发布列表"""
from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .binary_manager import BinaryManager  # noqa: E402


class ReleaseRow(Adw.ActionRow):
    def __init__(self, page: "VersionsPage", release: dict):
        super().__init__()
        self.tag = release["tag"]
        self.set_title(GLib.markup_escape_text(self.tag))
        parts = [release["published"] or ""]
        if release["prerelease"]:
            parts.append("预发布")
        if not release["has_asset"]:
            parts.append(f"无 {page.bm.asset} 资源")
        self.set_subtitle(" · ".join(p for p in parts if p))

        self.stack = Gtk.Stack(valign=Gtk.Align.CENTER, hhomogeneous=False)
        dl_btn = Gtk.Button(
            icon_name="folder-download-symbolic",
            tooltip_text="下载并安装此版本",
            css_classes=["flat"],
            sensitive=release["has_asset"],
        )
        dl_btn.connect("clicked", lambda *_: page.download(self.tag))

        prog_box = Gtk.Box(spacing=6)
        self.progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=150, show_text=True)
        cancel_btn = Gtk.Button(icon_name="process-stop-symbolic", css_classes=["flat"], tooltip_text="取消")
        cancel_btn.connect("clicked", lambda *_: page.bm.cancel(self.tag))
        prog_box.append(self.progress)
        prog_box.append(cancel_btn)

        done_box = Gtk.Box(spacing=4)
        done_box.append(Gtk.Image.new_from_icon_name("emblem-ok-symbolic"))
        done_box.append(Gtk.Label(label="已安装"))
        done_box.add_css_class("success")

        self.stack.add_named(dl_btn, "idle")
        self.stack.add_named(prog_box, "progress")
        self.stack.add_named(done_box, "done")
        self.add_suffix(self.stack)

    def set_installed(self, installed: bool) -> None:
        self.stack.set_visible_child_name("done" if installed else "idle")

    def set_progress(self, fraction: float) -> None:
        self.stack.set_visible_child_name("progress")
        if fraction < 0:
            self.progress.pulse()
            self.progress.set_text("下载中…")
        else:
            self.progress.set_fraction(fraction)
            self.progress.set_text(f"{fraction * 100:.0f}%")


class VersionsPage(Adw.PreferencesPage):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.bm: BinaryManager = win.bm
        self.config = win.config
        self._installed_rows: list[Gtk.Widget] = []
        self._release_widgets: list[Gtk.Widget] = []
        self._release_rows: dict[str, ReleaseRow] = {}
        self._radio_leader: Optional[Gtk.CheckButton] = None
        self._suppress_select = False
        self.latest_tag: Optional[str] = None
        self._install_after_fetch = False

        # ---------- 当前使用
        self.g_current = Adw.PreferencesGroup(
            title="当前使用的 cloudflared",
            description=f"平台 {self.bm.system}/{self.bm.arch} · 资源名 {self.bm.asset}",
        )
        self.current_row = Adw.ActionRow(title="检测中…")
        self.current_icon = Gtk.Image.new_from_icon_name("application-x-executable-symbolic")
        self.current_row.add_prefix(self.current_icon)
        refresh_cur = Gtk.Button(
            icon_name="view-refresh-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
            tooltip_text="重新检测",
        )
        refresh_cur.connect("clicked", lambda *_: self.win.on_binary_changed())
        self.current_row.add_suffix(refresh_cur)
        self.g_current.add(self.current_row)
        self.add(self.g_current)

        # ---------- 已安装
        self.g_installed = Adw.PreferencesGroup(
            title="版本选择", description="决定启动隧道时使用哪个 cloudflared 二进制文件。"
        )
        open_btn = Gtk.Button(
            icon_name="folder-open-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
            tooltip_text="打开安装目录",
        )
        open_btn.connect("clicked", self._open_dir)
        self.g_installed.set_header_suffix(open_btn)
        self.add(self.g_installed)

        # ---------- GitHub
        self.g_releases = Adw.PreferencesGroup(
            title="GitHub 发布版本", description="来源：cloudflare/cloudflared 官方仓库"
        )
        hbox = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.latest_btn = Gtk.Button(label="安装最新版", css_classes=["suggested-action"])
        self.latest_btn.connect("clicked", lambda *_: self.install_latest())
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", css_classes=["flat"], tooltip_text="刷新列表")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh_releases())
        self.spinner = Gtk.Spinner(visible=False)
        hbox.append(self.spinner)
        hbox.append(self.latest_btn)
        hbox.append(self.refresh_btn)
        self.g_releases.set_header_suffix(hbox)
        self.add(self.g_releases)
        self._set_release_placeholder("点击右侧刷新按钮获取版本列表", "network-transmit-receive-symbolic")

        self._rebuild_installed()

    # ------------------------------------------------------------ 当前
    def set_current(self, version: Optional[str], path: Optional[str]) -> None:
        if path:
            self.current_row.set_title(GLib.markup_escape_text(f"cloudflared {version or '(版本未知)'}"))
            self.current_row.set_subtitle(GLib.markup_escape_text(path))
            self.current_icon.set_from_icon_name("emblem-ok-symbolic")
            self.current_row.remove_css_class("error")
        else:
            self.current_row.set_title("未找到可用的 cloudflared")
            self.current_row.set_subtitle("请在下方安装一个版本，或将 cloudflared 放入 PATH")
            self.current_icon.set_from_icon_name("dialog-error-symbolic")
            self.current_row.add_css_class("error")

    # ------------------------------------------------------------ 已安装
    def _rebuild_installed(self) -> None:
        for row in self._installed_rows:
            self.g_installed.remove(row)
        self._installed_rows.clear()
        self._radio_leader = None

        active = self.config.get("active_version", "latest-installed")
        installed = self.bm.installed()
        sysbin = self.bm.system_binary()

        self._suppress_select = True
        self._add_choice(
            "latest-installed",
            "自动：最新已安装版本",
            f"当前解析为 {installed[0]}" if installed else "尚未安装任何版本，将回退到系统 PATH",
            active == "latest-installed",
        )
        self._add_choice(
            "system", "系统 PATH 中的 cloudflared", sysbin or "未在 PATH 中找到",
            active == "system", sensitive=bool(sysbin),
        )
        for tag in installed:
            self._add_choice(tag, tag, str(self.bm.path_for(tag)), active == tag, deletable=True)
        self._suppress_select = False

    def _add_choice(self, key: str, title: str, subtitle: str, checked: bool,
                    sensitive: bool = True, deletable: bool = False) -> None:
        row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle))
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER, sensitive=sensitive)
        if self._radio_leader is None:
            self._radio_leader = check
        else:
            check.set_group(self._radio_leader)
        check.set_active(checked)
        check.connect("toggled", lambda c: c.get_active() and self._select(key))
        row.add_prefix(check)
        row.set_activatable_widget(check)
        if deletable:
            del_btn = Gtk.Button(
                icon_name="user-trash-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
                tooltip_text="删除此版本",
            )
            del_btn.connect("clicked", lambda *_: self._confirm_delete(key))
            row.add_suffix(del_btn)
        self.g_installed.add(row)
        self._installed_rows.append(row)

    def _select(self, key: str) -> None:
        if self._suppress_select or self.config.get("active_version") == key:
            return
        self.config.set("active_version", key)
        self.config.save()
        self.win.toast("已切换 cloudflared 版本策略，新启动的隧道将使用新版本")
        self.win.on_binary_changed()

    def _confirm_delete(self, tag: str) -> None:
        dlg = Adw.MessageDialog(
            transient_for=self.win, heading=f"删除 cloudflared {tag}？",
            body="正在使用此版本运行的隧道不会被中断，但之后将无法再用它启动。",
        )
        dlg.add_response("cancel", "取消")
        dlg.add_response("delete", "删除")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_response(_d, resp):
            if resp != "delete":
                return
            self.bm.remove(tag)
            if self.config.get("active_version") == tag:
                self.config.set("active_version", "latest-installed")
                self.config.save()
            self._rebuild_installed()
            self._sync_release_rows()
            self.win.on_binary_changed()
            self.win.toast(f"已删除 cloudflared {tag}")

        dlg.connect("response", on_response)
        dlg.present()

    def _open_dir(self, *_args) -> None:
        uri = Gio.File.new_for_path(str(self.bm.versions_dir)).get_uri()
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as exc:
            self.win.toast(f"无法打开目录: {exc.message}")

    # ------------------------------------------------------------ 发布列表
    def _clear_release_widgets(self) -> None:
        for w in self._release_widgets:
            self.g_releases.remove(w)
        self._release_widgets.clear()
        self._release_rows.clear()

    def _set_release_placeholder(self, text: str, icon: str) -> None:
        self._clear_release_widgets()
        row = Adw.ActionRow(title=GLib.markup_escape_text(text))
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        self.g_releases.add(row)
        self._release_widgets.append(row)

    def refresh_releases(self) -> None:
        self.refresh_btn.set_sensitive(False)
        self.spinner.set_visible(True)
        self.spinner.start()
        self.bm.fetch_releases(self._on_releases)

    def _on_releases(self, releases: Optional[list], error: Optional[str]) -> None:
        self.refresh_btn.set_sensitive(True)
        self.spinner.stop()
        self.spinner.set_visible(False)
        if error or not releases:
            self._set_release_placeholder(f"获取失败：{error or '返回为空'}", "dialog-error-symbolic")
            self.win.toast("获取 GitHub 版本列表失败")
            self._install_after_fetch = False
            return
        self._clear_release_widgets()
        self.latest_tag = next(
            (r["tag"] for r in releases if r["has_asset"] and not r["prerelease"]), None
        )
        for rel in releases:
            row = ReleaseRow(self, rel)
            self.g_releases.add(row)
            self._release_widgets.append(row)
            self._release_rows[rel["tag"]] = row
        self._sync_release_rows()
        if self.latest_tag:
            self.g_releases.set_description(f"来源：cloudflare/cloudflared 官方仓库 · 最新稳定版 {self.latest_tag}")
            installed = self.bm.installed()
            if installed and not self.bm.is_installed(self.latest_tag):
                self.win.toast(f"有新版本可用：{self.latest_tag}（已安装 {installed[0]}）")
        if self._install_after_fetch:
            self._install_after_fetch = False
            self.install_latest()

    def _sync_release_rows(self) -> None:
        for tag, row in self._release_rows.items():
            if not self.bm.is_downloading(tag):
                row.set_installed(self.bm.is_installed(tag))

    def install_latest(self) -> None:
        if self.latest_tag:
            if self.bm.is_installed(self.latest_tag):
                self.win.toast(f"最新版 {self.latest_tag} 已安装")
            else:
                self.download(self.latest_tag)
        else:
            self._install_after_fetch = True
            self.refresh_releases()

    def download(self, tag: str) -> None:
        if self.bm.is_installed(tag) or self.bm.is_downloading(tag):
            return
        row = self._release_rows.get(tag)
        if row:
            row.set_progress(0.0)
        self.bm.download(tag, self._on_progress, self._on_done)

    def _on_progress(self, tag: str, fraction: float) -> None:
        row = self._release_rows.get(tag)
        if row:
            row.set_progress(fraction)

    def _on_done(self, tag: str, error: Optional[str]) -> None:
        if error:
            self.win.toast(f"安装 {tag} 失败：{error}")
        else:
            self.win.toast(f"cloudflared {tag} 安装完成")
        self._rebuild_installed()
        self._sync_release_rows()
        self.win.on_binary_changed()
