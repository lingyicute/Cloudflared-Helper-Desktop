#!/usr/bin/env python3
"""GTK regression tests: run with xvfb-run -a python3 tests/ui_regressions.py."""
import sys
from collections import deque
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi

gi.require_version('Adw', '1')
gi.require_version('Gtk', '4.0')
from gi.repository import Adw, Gio, GLib, Gtk

from cftm.app import MainWindow, TunnelRow, _window_maximized, _window_size
from cftm.binary_manager import BinaryManager
from cftm.config import Config
from cftm.dialogs import LogWindow, TunnelDialog, copy_text
from cftm.tunnel import TunnelConfig, TunnelProcess
from cftm.versions_page import ReleaseRow, VersionsPage


class UIRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Adw.init()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.win = Mock()
        self.win.config = Config(Path(self.tmp.name) / 'config.json')
        self.win.bm = BinaryManager(Path(self.tmp.name) / 'versions')

    def test_tunnel_names_are_literal_text(self):
        name = 'NAS & <b>not bold</b>'
        row = TunnelRow(TunnelProcess(TunnelConfig(name=name, hostname='x.example.com')), self.win)
        self.addCleanup(row.dispose_row)
        self.assertFalse(row.get_use_markup())
        self.assertEqual(row.get_title(), name)

    def test_release_tags_and_paths_are_literal_text(self):
        page = VersionsPage(self.win)
        release = dict(tag='2026.1.0-<b>test</b>', published='', prerelease=True, has_asset=True)
        row = ReleaseRow(page, release)
        self.assertFalse(row.get_use_markup())
        page._add_choice('test', 'A & B', '/tmp/<test>/cloudflared', False)
        self.assertFalse(page._installed_rows[-1].get_use_markup())
        self.assertFalse(page.current_row.get_use_markup())
        page._set_release_placeholder('Failure: <error> & retry', 'dialog-error-symbolic')
        self.assertFalse(page._release_widgets[0].get_use_markup())

    def test_log_buffer_stays_bounded(self):
        parent = Gtk.Window()
        self.addCleanup(parent.destroy)
        proc = TunnelProcess(TunnelConfig(hostname='x.example.com'))
        window = LogWindow(parent, proc)
        self.addCleanup(window.destroy)
        window.follow_btn.set_active(False)
        for n in range(6500):
            window._queue_append(f'line {n}')
        window._cancel_flush()
        window._flush_pending()
        self.assertLessEqual(window.buffer.get_line_count(), window.MAX_LINES + window.TRIM_SLACK)
        text = window.buffer.get_text(*window.buffer.get_bounds(), False)
        self.assertIn('line 6499', text)
        self.assertNotIn('line 0\n', text)
        window._on_close()

    def test_log_updates_are_batched_and_clear_discards_pending(self):
        # 只需 GTK TextBuffer，不依赖较新 libadwaita 的 ToolbarView。
        window = Mock()
        window.buffer = Gtk.TextBuffer()
        window.MAX_LINES = LogWindow.MAX_LINES
        window.TRIM_SLACK = LogWindow.TRIM_SLACK
        window._pending_lines = deque(maxlen=window.MAX_LINES)
        window._flush_source = None
        window._flush_pending = lambda: LogWindow._flush_pending(window)
        window._cancel_flush = lambda: LogWindow._cancel_flush(window)
        window._trim_to = lambda n: LogWindow._trim_to(window, n)
        window.follow_btn.get_active.return_value = False
        for n in range(6500):
            LogWindow._queue_append(window, f'line {n}')
        self.assertLessEqual(len(window._pending_lines), window.MAX_LINES)
        self.assertEqual(window.buffer.get_text(*window.buffer.get_bounds(), False), '')
        window._cancel_flush()
        window._flush_pending()
        text = window.buffer.get_text(*window.buffer.get_bounds(), False)
        self.assertIn('line 6499', text)
        self.assertNotIn('line 0\n', text)
        self.assertLessEqual(window.buffer.get_line_count(), window.MAX_LINES + window.TRIM_SLACK)
        LogWindow._queue_append(window, 'stale line')
        LogWindow._clear(window)
        self.assertIsNone(window._flush_source)
        window._flush_pending()
        self.assertEqual(window.buffer.get_text(*window.buffer.get_bounds(), False), '')

    def test_edit_reuses_tunnel_row(self):
        win = Mock()
        proc = TunnelProcess(TunnelConfig(name='before', hostname='x.example.com'))
        win.manager.procs = [proc]
        win._rows = []
        win.listbox = Gtk.ListBox()
        MainWindow._rebuild_list(win)
        row = win._rows[0]
        proc.config = proc.config.copy()
        proc.config.name = 'after'
        MainWindow._rebuild_list(win)
        self.assertIs(win._rows[0], row)
        self.assertEqual(row.get_title(), 'after')
        win.manager.procs = []
        MainWindow._rebuild_list(win)
        self.assertEqual(row._hid, 0)
        self.assertEqual(len(win._rows), 0)

    def test_rebuild_keeps_listbox_in_manager_order(self):
        win = Mock()
        a = TunnelProcess(TunnelConfig(name='a', hostname='a.example.com'))
        b = TunnelProcess(TunnelConfig(name='b', hostname='b.example.com'))
        c = TunnelProcess(TunnelConfig(name='c', hostname='c.example.com'))
        win.manager.procs = [a, c]
        win._rows = []
        win.listbox = Gtk.ListBox()
        MainWindow._rebuild_list(win)
        win.manager.procs = [a, b, c]
        MainWindow._rebuild_list(win)
        self.assertEqual([r.proc for r in win._rows], [a, b, c])
        self.assertEqual([win.listbox.get_row_at_index(i) for i in range(3)], win._rows)
        win.manager.procs = [c, a, b]
        MainWindow._rebuild_list(win)
        self.assertEqual([win.listbox.get_row_at_index(i).proc for i in range(3)], [c, a, b])

    def test_editor_validation_is_transactional(self):
        parent = Gtk.Window()
        self.addCleanup(parent.destroy)
        cfg = TunnelConfig(hostname='x.example.com', name='original')
        saved = []
        dialog = TunnelDialog(parent, cfg, saved.append)
        self.addCleanup(dialog.destroy)
        dialog.name_row.set_text('modified')
        dialog.listen_row.set_text('http://localhost')
        dialog._on_save_clicked()
        self.assertFalse(saved)
        self.assertEqual(cfg.name, 'original')
        dialog.listen_row.set_text('::1')
        dialog._on_save_clicked()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].build_argv('cloudflared')[-1], '[::1]:21128')

    def test_restored_window_height_keeps_the_short_minimum(self):
        # 高度下限是 460（见 set_size_request），不能被宽度的 640 一起夹紧
        self.assertEqual(_window_size({'width': 900, 'height': 500}), (900, 500))
        self.assertEqual(_window_size({'width': 1, 'height': 1}), (640, 460))
        self.assertEqual(_window_size({'width': None, 'height': 'tall'}), (980, 660))
        self.assertEqual(_window_size(None), (980, 660))

    def test_duplicate_toasts_only_when_the_port_was_moved(self):
        win = Mock()
        source = Mock()
        source.config.port = 2222
        win.manager.get.return_value = source
        moved = Mock()
        moved.config.port = 2223
        win.manager.duplicate.return_value = moved
        MainWindow._duplicate(win, 'abc')
        win.toast.assert_called_once()
        self.assertIn('2223', win.toast.call_args[0][0])
        win.toast.reset_mock()
        same = Mock()
        same.config.port = 2222
        win.manager.duplicate.return_value = same
        MainWindow._duplicate(win, 'abc')
        win.toast.assert_not_called()

    def test_copy_text_reports_whether_the_clipboard_accepted_the_text(self):
        parent = Gtk.Window()
        self.addCleanup(parent.destroy)
        self.assertIsInstance(copy_text(parent, 'cloudflared access tcp'), bool)
        broken = Mock()
        broken.get_clipboard.side_effect = RuntimeError('no display')
        self.assertFalse(copy_text(broken, 'cloudflared access tcp'))
        refused = Mock()
        refused.get_clipboard.return_value.set_content.return_value = False
        self.assertFalse(copy_text(refused, 'cloudflared access tcp'))
        no_clipboard = Mock()
        no_clipboard.get_clipboard.return_value = None
        self.assertFalse(copy_text(no_clipboard, 'cloudflared access tcp'))

    def test_quick_tunnel_switch_ignores_truthy_strings(self):
        # 手改 config.json 写成 "false" 时不能因为 bool("false") 为 True 而读取剪贴板
        win = Mock()
        for raw, want in (('false', False), ('off', False), (0, False), (False, False),
                          ('true', True), (True, True), (None, True)):
            with self.subTest(raw=raw):
                win.config.get.return_value = raw
                self.assertIs(MainWindow._quick_tunnel_enabled(win), want)

    def test_window_state_keeps_restored_size_and_maximized_flag(self):
        # 最大化退出时要保存还原后的尺寸，而不是铺满屏幕的当前尺寸
        win = Mock()
        win.config = self.win.config
        win.get_default_size.return_value = (1000, 700)
        win.get_width.return_value, win.get_height.return_value = 1920, 1080
        win.is_maximized.return_value = True
        MainWindow._save_window_size(win)
        saved = Config(win.config.path).get('window')
        self.assertEqual(saved, {'width': 1000, 'height': 700, 'maximized': True})
        self.assertEqual(_window_size(saved), (1000, 700))
        self.assertTrue(_window_maximized(saved))
        for raw in (None, {}, {'maximized': 'false'}, {'maximized': None}):
            with self.subTest(raw=raw):
                self.assertFalse(_window_maximized(raw))

    def test_copy_all_logs_reports_the_clipboard_result(self):
        window = Mock()
        window.buffer = Gtk.TextBuffer()
        window._pending_lines = deque(maxlen=LogWindow.MAX_LINES)
        window._flush_source = None
        window._flush_pending = lambda: LogWindow._flush_pending(window)
        window._cancel_flush = lambda: LogWindow._cancel_flush(window)
        with patch('cftm.dialogs.copy_text', return_value=True) as copy:
            LogWindow._copy_all(window)
            copy.assert_not_called()  # 空日志不覆盖用户剪贴板
            window._toast.assert_called_once()
            window.buffer.set_text('line 1\n')
            window._toast.reset_mock()
            LogWindow._copy_all(window)
            copy.assert_called_once_with(window, 'line 1\n')
            window._toast.assert_called_once_with('日志已复制到剪贴板')
            copy.return_value = False
            window._toast.reset_mock()
            LogWindow._copy_all(window)
            self.assertIn('失败', window._toast.call_args[0][0])

    def test_open_dir_reports_failure_only_when_fallback_fails_too(self):
        page = Mock()
        page.bm = self.win.bm
        with patch.object(Gio.AppInfo, 'launch_default_for_uri', side_effect=GLib.Error('no handler')), \
                patch.object(Gio.Subprocess, 'new') as spawn:
            VersionsPage._open_dir(page)
            spawn.assert_called_once()
            page.win.toast.assert_not_called()
            spawn.side_effect = GLib.Error('no xdg-open')
            VersionsPage._open_dir(page)
            page.win.toast.assert_called_once()
            self.assertIn('no handler', page.win.toast.call_args[0][0])

    def test_version_delete_reports_failure_honestly(self):
        page = Mock()
        page.config = self.win.config
        page.config.set('active_version', '2026.1.0')
        page.bm.remove.return_value = False
        VersionsPage._delete_version(page, '2026.1.0')
        self.assertIn('失败', page.win.toast.call_args.args[0])
        self.assertEqual(page.config.get('active_version'), '2026.1.0')
        page.bm.remove.return_value = True
        VersionsPage._delete_version(page, '2026.1.0')
        self.assertEqual(page.win.toast.call_args.args[0], '已删除 cloudflared 2026.1.0')
        self.assertEqual(page.config.get('active_version'), 'latest-installed')


if __name__ == '__main__':
    unittest.main()
