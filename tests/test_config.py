"""Configuration persistence regressions; no display required."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from cftm.config import Config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.json'

    def test_invalid_utf8_uses_defaults(self):
        self.path.write_bytes(b'\xff')
        self.assertEqual(Config(self.path).get('tunnels'), [])

    def test_unreadable_config_is_set_aside_not_overwritten(self):
        raws = (b'{"tunnels": [},', b'\xff', b'[]')
        for raw in raws:
            self.path.write_bytes(raw)
            cfg = Config(self.path)
            self.assertEqual(cfg.get('tunnels'), [])
            self.assertFalse(self.path.exists())
            cfg.save()
            self.assertEqual(json.loads(self.path.read_text()), cfg.data)
        backups = sorted(self.path.parent.glob('config.json.corrupt-*'))
        self.assertEqual(sorted(b.read_bytes() for b in backups), sorted(raws))

    def test_unopenable_config_is_set_aside_not_overwritten(self):
        raw = '{"tunnels": [{"hostname": "keep.example.com"}]}'
        self.path.write_text(raw)
        with patch('cftm.config.open', side_effect=PermissionError('denied'), create=True):
            cfg = Config(self.path)
        self.assertEqual(cfg.get('tunnels'), [])
        cfg.save()
        backups = list(self.path.parent.glob('config.json.corrupt-*'))
        self.assertEqual([b.read_text() for b in backups], [raw])

    def test_untraversable_config_dir_does_not_abort_startup(self):
        denied = PermissionError('denied')
        with patch('cftm.config.open', side_effect=denied, create=True), \
             patch.object(Path, 'exists', side_effect=denied):
            self.assertEqual(Config(self.path).get('tunnels'), [])

    def test_deeply_nested_config_is_set_aside_not_fatal(self):
        raw = b'[' * 100000
        self.path.write_bytes(raw)
        self.assertEqual(Config(self.path).get('tunnels'), [])
        backups = list(self.path.parent.glob('config.json.corrupt-*'))
        self.assertEqual([b.read_bytes() for b in backups], [raw])

    def test_valid_config_is_not_set_aside(self):
        self.path.write_text('{"active_version": "system"}')
        self.assertEqual(Config(self.path).get('active_version'), 'system')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_utf8_bom_config_is_loaded_not_set_aside(self):
        self.path.write_bytes(b'\xef\xbb\xbf{"active_version": "system"}')
        self.assertEqual(Config(self.path).get('active_version'), 'system')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_save_does_not_follow_predictable_temp_symlink(self):
        victim = self.path.parent / 'other-file'
        victim.write_text('keep me')
        self.path.with_suffix('.json.tmp').symlink_to(victim)
        cfg = Config(self.path)
        cfg.save()
        self.assertEqual(victim.read_text(), 'keep me')
        self.assertEqual(json.loads(self.path.read_text()), cfg.data)

    def test_save_is_private(self):
        Config(self.path).save()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == 'posix', '目录 fsync 仅适用于 POSIX')
    def test_save_syncs_the_directory_after_the_rename(self):
        """文件内容与目录项都要 fsync 过，rename 结果才能在断电后保住。"""
        cfg = Config(self.path)
        cfg.set('active_version', '2026.1.0')
        events = []
        real_fsync, real_replace = os.fsync, Path.replace

        def fsync(fd):
            events.append('fsync-dir' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'fsync-file')
            return real_fsync(fd)

        def replace(src, dst):
            events.append('replace')
            return real_replace(src, dst)

        with patch('cftm.config.os.fsync', side_effect=fsync), \
                patch.object(Path, 'replace', replace):
            self.assertTrue(cfg.save())
        self.assertEqual(events, ['fsync-file', 'replace', 'fsync-dir'])
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['active_version'], '2026.1.0')

    @unittest.skipUnless(os.name == 'posix', '目录 fsync 仅适用于 POSIX')
    def test_directory_sync_failure_does_not_fail_the_save(self):
        cfg = Config(self.path)
        cfg.set('active_version', '2026.1.0')
        real_fsync = os.fsync

        def fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('directory fsync unsupported')
            return real_fsync(fd)

        errors = []
        cfg.on_save_error = errors.append
        with patch('cftm.config.os.fsync', side_effect=fsync):
            self.assertTrue(cfg.save())
        self.assertEqual(errors, [])
        self.assertTrue(cfg.last_save_ok)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['active_version'], '2026.1.0')

    def test_failed_replace_preserves_original_and_cleans_temp(self):
        self.path.write_text('{"active_version": "system"}')
        cfg = Config(self.path)
        cfg.set('active_version', 'latest-installed')
        with patch.object(Path, 'replace', side_effect=OSError('disk error')):
            cfg.save()
        self.assertEqual(json.loads(self.path.read_text())['active_version'], 'system')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_save_reports_the_outcome_to_the_ui(self):
        cfg = Config(self.path)
        reasons = []
        cfg.on_save_error = reasons.append
        self.assertTrue(cfg.save())
        self.assertTrue(cfg.last_save_ok)
        with patch.object(Path, 'replace', side_effect=OSError('disk error')):
            self.assertFalse(cfg.save())
            self.assertFalse(cfg.last_save_ok)
            # 回调自己出错也不能让 save() 抛异常
            cfg.on_save_error = lambda reason: 1 / 0
            self.assertFalse(cfg.save())
        self.assertEqual(reasons, ['disk error'])
        self.assertTrue(cfg.save())
        self.assertTrue(cfg.last_save_ok)

    def test_failed_encoding_preserves_original(self):
        self.path.write_text('{}')
        cfg = Config(self.path)
        cfg.set('bad', '\ud800')
        cfg.save()
        self.assertEqual(self.path.read_text(), '{}')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_defaults_are_independent(self):
        first, second = Config(self.path), Config(self.path)
        first.get('tunnels').append({'name': 'one'})
        self.assertEqual(second.get('tunnels'), [])

    def test_reload_resets_removed_keys_to_defaults(self):
        cfg = Config(self.path)
        cfg.set('custom_key', 123)
        self.path.write_text('{"active_version": "system"}')
        cfg.load()
        self.assertNotIn('custom_key', cfg.data)
        self.assertEqual(cfg.get('active_version'), 'system')

    def test_xdg_ignores_relative_paths_and_strips_whitespace(self):
        from cftm.config import _xdg
        with patch.dict('os.environ', {'XDG_CONFIG_HOME': 'relative/path'}):
            self.assertEqual(_xdg('XDG_CONFIG_HOME', '.config'), Path.home() / '.config')
        with patch.dict('os.environ', {'XDG_CONFIG_HOME': f'  {self.tmp.name}  '}):
            self.assertEqual(_xdg('XDG_CONFIG_HOME', '.config'), Path(self.tmp.name))

    def test_set_aside_skips_dangling_symlink_backup(self):
        with patch('cftm.config.time.strftime', return_value='20260101-000000'):
            dangling = self.path.with_name('config.json.corrupt-20260101-000000')
            dangling.symlink_to(self.path.parent / 'missing-target')
            self.path.write_bytes(b'{bad')
            Config(self.path)
            numbered = self.path.with_name('config.json.corrupt-20260101-000000-1')
            self.assertTrue(dangling.is_symlink())
            self.assertEqual(numbered.read_bytes(), b'{bad')

    def test_set_aside_never_overwrites_existing_backups(self):
        stamp = '20260101-000000'
        base = self.path.with_name(f'config.json.corrupt-{stamp}')
        existing = {}
        for n in range(100):
            backup = base if n == 0 else base.with_name(f'{base.name}-{n}')
            existing[backup] = f'previous backup {n}'.encode()
            backup.write_bytes(existing[backup])
        self.path.write_bytes(b'{broken')

        with patch('cftm.config.time.strftime', return_value=stamp):
            cfg = Config(self.path)

        self.assertFalse(self.path.exists())
        self.assertEqual(
            base.with_name(f'{base.name}-100').read_bytes(), b'{broken'
        )
        for backup, data in existing.items():
            self.assertEqual(backup.read_bytes(), data)
        self.assertEqual(cfg.get('tunnels'), [])


class IconFallbackTests(unittest.TestCase):
    """图标回退到系统 hicolor 目录时，不能把资源目录也改成图标目录。"""

    def run_config(self, existing):
        import runpy

        real_is_file = Path.is_file
        with patch.object(Path, 'is_file', lambda p: str(p) in existing or (
                not str(p).endswith('.svg') and real_is_file(p))):
            return runpy.run_path(str(Path(__file__).resolve().parent.parent / 'cftm' / 'config.py'))

    def test_icon_fallback_keeps_resource_data_dir(self):
        from cftm.config import APP_ID, RESOURCE_DATA_DIR

        for prefix in ('/app', '/usr'):
            icon = f'{prefix}/share/icons/hicolor/scalable/apps/{APP_ID}.svg'
            with self.subTest(prefix=prefix):
                ns = self.run_config({icon})
                self.assertEqual(ns['APP_ICON_FILE'], Path(icon))
                self.assertEqual(ns['RESOURCE_DATA_DIR'], RESOURCE_DATA_DIR)


class StrictBoolTests(unittest.TestCase):
    def test_only_explicit_switch_values_are_honoured(self):
        from cftm.config import strict_bool
        cases = [(True, True), (False, False), (1, True), (0, False), (0.0, False),
                 ('true', True), (' ON ', True), ('yes', True), ('1', True),
                 ('false', False), ('Off', False), ('no', False), ('0', False), ('', False)]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertIs(strict_bool(raw, default=True), want)
                self.assertIs(strict_bool(raw, default=False), want)

    def test_nan_falls_back_to_default(self):
        from cftm.config import strict_bool
        for default in (True, False):
            self.assertIs(strict_bool(float('nan'), default=default), default)
        # 手改的 config.json 里写 NaN，json 解析出来的正是 float('nan')
        self.assertIs(strict_bool(json.loads('NaN'), default=False), False)

    def test_unrecognised_values_fall_back_to_default(self):
        from cftm.config import strict_bool
        for raw in (None, [], {}, 'maybe', {'on': True}):
            with self.subTest(raw=raw):
                self.assertIs(strict_bool(raw, default=True), True)
                self.assertIs(strict_bool(raw), False)
