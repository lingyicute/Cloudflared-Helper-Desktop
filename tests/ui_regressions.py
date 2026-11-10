#!/usr/bin/env python3
"""GTK regression tests: run with xvfb-run -a python3 tests/ui_regressions.py."""
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi

gi.require_version('Adw', '1')
gi.require_version('Gtk', '4.0')
from gi.repository import Adw, Gtk

from cftm.app import MainWindow, TunnelRow, _window_size
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
            window._append(f'line {n}')
        self.assertLessEqual(window.buffer.get_line_count(), window.MAX_LINES + window.TRIM_SLACK)
        text = window.buffer.get_text(*window.buffer.get_bounds(), False)
        self.assertIn('line 6499', text)
        self.assertNotIn('line 0\n', text)
        window._on_close()

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


if __name__ == '__main__':
    unittest.main()
