"""Offline binary selection and installation tests (real files, mocked HTTP)."""
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from cftm.binary_manager import BinaryManager, format_size, version_key


class InlineThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class Response(io.BytesIO):
    def __init__(self, data, length=None, on_eof=None):
        super().__init__(data)
        self.headers = {} if length is None else {'Content-Length': str(length)}
        self.on_eof = on_eof

    def read(self, size=-1):
        data = super().read(size)
        if not data and self.on_eof:
            self.on_eof()
        return data


class BinaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bm = BinaryManager(Path(self.tmp.name) / 'versions')
        self.bm.system, self.bm.arch, self.bm.asset = 'linux', 'amd64', 'cloudflared-linux-amd64'
        self.tag = '2026.1.0'

    def install_old(self, content=b'old'):
        final = self.bm.path_for(self.tag)
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(content)
        final.chmod(0o755)
        return final

    def download(self, response, **kwargs):
        done = []
        with patch('cftm.binary_manager.threading.Thread', InlineThread), \
             patch('cftm.binary_manager.urllib.request.urlopen', return_value=response), \
             patch('cftm.binary_manager.GLib.idle_add', side_effect=lambda cb, *a: cb(*a)):
            self.bm.download(self.tag, lambda *_: None, lambda *a: done.append(a), **kwargs)
        self.assertEqual(len(done), 1)
        self.assertFalse(self.bm.is_downloading(self.tag))
        self.assertIsNone(self.bm.download_stats(self.tag))
        self.assertFalse(list(self.bm.versions_dir.rglob('*.part')))
        self.assertFalse(list(self.bm.versions_dir.rglob('*.extracting')))
        self.assertFalse(list(self.bm.versions_dir.glob('.install-*')))
        return done[0][1]

    def test_stable_beats_numbered_prerelease(self):
        tags = ['2026.1.0-rc.2', '2026.1.0', '2026.1.0-beta.10', '2026.1.0-rc.10']
        self.assertEqual(sorted(tags, key=version_key),
                         ['2026.1.0-beta.10', '2026.1.0-rc.2', '2026.1.0-rc.10', '2026.1.0'])
        self.assertEqual(version_key('v2026.1.0+build.9'), version_key('2026.1.0'))
        self.assertLess(version_key('2026.1.0-1'), version_key('2026.1.0'))
        self.assertLess(version_key('2026.1.0'), version_key('2026.2.0-rc.1'))

    def test_foreign_binary_and_directory_are_not_installed(self):
        final = self.bm.path_for(self.tag)
        final.parent.mkdir()
        final.with_suffix('.exe').write_bytes(b'PE')
        self.assertFalse(self.bm.is_installed(self.tag))
        final.mkdir()
        self.assertEqual(self.bm.installed(), [])
        with patch.object(self.bm, 'system_binary', return_value='/system/cloudflared'):
            self.assertEqual(self.bm.resolve('latest-installed'), '/system/cloudflared')

    def test_non_executable_is_not_installed(self):
        final = self.install_old()
        final.chmod(0o644)
        self.assertFalse(self.bm.is_installed(self.tag))

    def test_success_with_digest_and_size(self):
        data = b'new binary'
        self.assertIsNone(self.download(Response(data), size=len(data),
                                       digest='sha256:' + hashlib.sha256(data).hexdigest()))
        self.assertEqual(self.bm.path_for(self.tag).read_bytes(), data)
        self.assertEqual(self.bm.installed(), [self.tag])

    def test_integrity_failures_preserve_old(self):
        final = self.install_old()
        for response, kwargs in [(Response(b'new', 20), {}), (Response(b''), {}),
                                 (Response(b'new'), {'size': 20}),
                                 (Response(b'new'), {'digest': 'sha256:' + '0' * 64})]:
            with self.subTest(kwargs=kwargs):
                self.assertIsNotNone(self.download(response, **kwargs))
                self.assertEqual(final.read_bytes(), b'old')

    def test_cancel_during_eof_does_not_publish(self):
        final = self.install_old()
        error = self.download(Response(b'new', on_eof=lambda: self.bm.cancel(self.tag)))
        self.assertEqual(error, '已取消')
        self.assertEqual(final.read_bytes(), b'old')

    def test_chmod_failure_preserves_old(self):
        final = self.install_old()
        with patch.object(Path, 'chmod', side_effect=OSError('permission denied')):
            self.assertIsNotNone(self.download(Response(b'new')))
        self.assertEqual(final.read_bytes(), b'old')

    def archive(self, name='cloudflared', kind=tarfile.REGTYPE):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as tf:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = 'elsewhere' if kind != tarfile.REGTYPE else ''
            member.size = 3 if kind == tarfile.REGTYPE else 0
            tf.addfile(member, io.BytesIO(b'new') if member.size else None)
        self.bm.system, self.bm.asset = 'darwin', 'cloudflared-darwin-amd64.tgz'
        return Response(data.getvalue())

    def test_download_size_limit_is_enforced(self):
        final = self.install_old()
        data = b'x' * 100
        with patch('cftm.binary_manager.MAX_DOWNLOAD_BYTES', 64):
            for response, kwargs in [(Response(data, len(data)), {}),   # 声明长度超限
                                     (Response(data), {'size': len(data)}),  # 发布信息超限
                                     (Response(data), {})]:             # 无长度信息的无限流
                with self.subTest(kwargs=kwargs, declared=response.headers):
                    error = self.download(response, **kwargs)
                    self.assertIn('超', error)
                    self.assertEqual(final.read_bytes(), b'old')
            # 恰好等于上限仍可安装
            self.assertIsNone(self.download(Response(data[:64]), size=64))
        self.assertEqual(self.bm.path_for(self.tag).read_bytes(), data[:64])

    def test_extracted_size_limit_is_enforced(self):
        final = self.install_old()
        with patch('cftm.binary_manager.MAX_EXTRACTED_BYTES', 2):
            error = self.download(self.archive('cloudflared'))   # 归档里的文件是 3 字节
            self.assertIn('过大', error)
            self.assertEqual(final.read_bytes(), b'old')
        with patch('cftm.binary_manager.MAX_EXTRACTED_BYTES', 3):
            self.assertIsNone(self.download(self.archive('cloudflared')))
        self.assertEqual(self.bm.path_for(self.tag).read_bytes(), b'new')

    def test_tgz_success(self):
        self.assertIsNone(self.download(self.archive('./cloudflared')))
        self.assertEqual(self.bm.path_for(self.tag).read_bytes(), b'new')

    def test_tgz_skips_directory_named_cloudflared(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as tf:
            directory = tarfile.TarInfo('cloudflared')
            directory.type = tarfile.DIRTYPE
            tf.addfile(directory)
            member = tarfile.TarInfo('cloudflared/cloudflared')
            member.size = 3
            tf.addfile(member, io.BytesIO(b'new'))
        self.bm.system, self.bm.asset = 'darwin', 'cloudflared-darwin-amd64.tgz'
        self.assertIsNone(self.download(Response(data.getvalue(), length=-1)))
        self.assertEqual(self.bm.path_for(self.tag).read_bytes(), b'new')

    def test_fetch_releases_filters_unsafe_and_draft_tags(self):
        payload = b'[{"tag_name":"../evil","assets":[]},{"tag_name":".hidden","assets":[]},' \
                  b'{"tag_name":"2026.1.0","draft":true,"assets":[]},' \
                  b'{"tag_name":"2026.2.0","published_at":"2026-02-01T00:00:00Z","assets":[{"name":"cloudflared-linux-amd64","size":10}]}]'
        got = []
        with patch('cftm.binary_manager.threading.Thread', InlineThread), \
             patch('cftm.binary_manager.urllib.request.urlopen', return_value=Response(payload)), \
             patch('cftm.binary_manager.GLib.idle_add', side_effect=lambda cb, *a: cb(*a)):
            self.bm.fetch_releases(lambda rels, err: got.append((rels, err)))
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0][1])
        self.assertEqual([r['tag'] for r in got[0][0]], ['2026.2.0'])

    def test_tgz_rejects_suffix_match_and_links(self):
        final = self.install_old()
        for name, kind in [('not-cloudflared', tarfile.REGTYPE),
                           ('cloudflared', tarfile.SYMTYPE), ('cloudflared', tarfile.LNKTYPE)]:
            with self.subTest(name=name, kind=kind):
                final = self.install_old()
                self.assertIsNotNone(self.download(self.archive(name, kind)))
                self.assertEqual(final.read_bytes(), b'old')

    def test_symlink_version_directory_is_not_written(self):
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        self.bm.path_for(self.tag).parent.symlink_to(outside, target_is_directory=True)
        self.assertIsNotNone(self.download(Response(b'new')))
        self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_tag_never_opens_network(self):
        self.tag = '../outside'
        with patch('cftm.binary_manager.urllib.request.urlopen') as request, \
             patch('cftm.binary_manager.GLib.idle_add', side_effect=lambda cb, *a: cb(*a)):
            done = []
            self.bm.download(self.tag, lambda *_: None, lambda *a: done.append(a))
        request.assert_not_called()
        self.assertEqual(len(done), 1)
        self.assertIsNotNone(done[0][1])

    def test_cancel_during_extraction_preserves_old(self):
        final = self.install_old()
        response = self.archive()
        original = tarfile.TarFile.extractfile

        def extract(tf, member):
            self.bm.cancel(self.tag)
            return original(tf, member)

        with patch.object(tarfile.TarFile, 'extractfile', extract):
            self.assertEqual(self.download(response), '已取消')
        self.assertEqual(final.read_bytes(), b'old')

    def test_done_callback_can_register_retry(self):
        queued = []

        class QueuedThread(InlineThread):
            def start(inner):
                queued.append(inner.target)

        done = []

        def on_done(tag, error):
            done.append(error)
            if len(done) == 1:
                self.bm.download(tag, lambda *_: None, on_done)

        with patch('cftm.binary_manager.threading.Thread', QueuedThread), \
             patch('cftm.binary_manager.urllib.request.urlopen', side_effect=[Response(b''), Response(b'ok')]), \
             patch('cftm.binary_manager.GLib.idle_add', side_effect=lambda cb, *a: cb(*a)):
            self.bm.download(self.tag, lambda *_: None, on_done)
            queued.pop(0)()
            self.assertTrue(self.bm.is_downloading(self.tag))
            queued.pop(0)()
        self.assertEqual(len(done), 2)
        self.assertIsNone(done[-1])
        self.assertFalse(self.bm.is_downloading(self.tag))

    def test_queued_progress_still_sees_stats_before_done_callback(self):
        idle_queue = []
        seen_stats = []
        done = []
        with patch('cftm.binary_manager.threading.Thread', InlineThread), \
             patch('cftm.binary_manager.urllib.request.urlopen', return_value=Response(b'ok', length=2)), \
             patch('cftm.binary_manager.GLib.idle_add', side_effect=lambda cb, *a: idle_queue.append((cb, a))):
            self.bm.download(
                self.tag,
                lambda tag, _f: seen_stats.append(self.bm.download_stats(tag)),
                lambda tag, err: done.append((tag, err)),
            )
            self.assertTrue(self.bm.is_downloading(self.tag))
            while idle_queue:
                cb, args = idle_queue.pop(0)
                cb(*args)
        self.assertTrue(seen_stats)
        self.assertTrue(all(s is not None and s['got'] == 2 for s in seen_stats))
        self.assertEqual(done, [(self.tag, None)])
        self.assertFalse(self.bm.is_downloading(self.tag))

    def test_probe_version_ignores_nonzero_exit_and_extracts_version(self):
        bad = Path(self.tmp.name) / 'bad-cf'
        bad.write_text('#!/bin/sh\necho "GLIBC_2.38: version 9.9.9 not found" >&2\nexit 1\n')
        bad.chmod(0o755)
        self.assertIsNone(BinaryManager.probe_version(str(bad)))

        good = Path(self.tmp.name) / 'good-cf'
        good.write_text('#!/bin/sh\necho "cloudflared version 2026.2.0 (built 2026-02-01)"\n')
        good.chmod(0o755)
        self.assertEqual(BinaryManager.probe_version(str(good)), '2026.2.0')

    def test_clean_stale_parts_removes_leftover_staging_dirs(self):
        staging = self.bm.versions_dir / '.install-stale'
        staging.mkdir()
        (staging / 'unexpected.tmp').write_bytes(b'leftover')
        BinaryManager(self.bm.versions_dir)
        self.assertFalse(staging.exists())

    def test_unreadable_versions_dir_is_treated_as_empty(self):
        target = Path(self.tmp.name) / 'unreadable'
        target.mkdir()
        bm = BinaryManager(target)
        with patch.object(Path, 'iterdir', side_effect=PermissionError('denied')):
            self.assertEqual(bm.installed(), [])
            with patch.object(bm, 'system_binary', return_value=None):
                self.assertIsNone(bm.resolve('latest-installed'))

    def test_untraversable_versions_dir_is_treated_as_empty(self):
        # 目录能列出但进不去：子项 stat 抛 PermissionError（用 mock 以便 root 下也能跑）
        target = Path(self.tmp.name) / 'untraversable'
        (target / '2026.1.0').mkdir(parents=True)
        bm = BinaryManager(target)
        with patch.object(Path, 'is_symlink', side_effect=PermissionError('denied')):
            self.assertFalse(bm.is_installed('2026.1.0'))
            self.assertEqual(bm.installed(), [])
            with patch.object(bm, 'system_binary', return_value=None):
                self.assertIsNone(bm.resolve('2026.1.0'))
                self.assertIsNone(bm.resolve('latest-installed'))

    def test_unwritable_versions_dir_does_not_abort_startup(self):
        target = Path(self.tmp.name) / 'readonly' / 'versions'
        with patch.object(Path, 'mkdir', side_effect=PermissionError('read-only')):
            bm = BinaryManager(target)
        self.assertEqual(bm.installed(), [])
        self.assertFalse(target.exists())
        self.assertFalse(bm.is_installed('2026.1.0'))
        with patch.object(Path, 'mkdir', side_effect=PermissionError('read-only')), \
             patch('cftm.binary_manager.threading.Thread', InlineThread), \
             patch('cftm.binary_manager.GLib.idle_add', side_effect=lambda cb, *a: cb(*a)):
            done = []
            bm.download('2026.1.0', lambda *_: None, lambda *a: done.append(a))
        self.assertEqual(len(done), 1)
        self.assertIsNotNone(done[0][1])

    def test_size_provides_progress_fallback_and_remove_unlinks_symlinks(self):
        fractions = []
        data = b'12345678'
        with patch('cftm.binary_manager.threading.Thread', InlineThread), \
             patch('cftm.binary_manager.urllib.request.urlopen', return_value=Response(data)), \
             patch('cftm.binary_manager.GLib.idle_add', side_effect=lambda cb, *a: cb(*a)):
            self.bm.download(self.tag, lambda _t, f: fractions.append(f), lambda *_: None, size=len(data))
        self.assertIn(1.0, fractions)

        outside = Path(self.tmp.name) / 'outside-dir'
        outside.mkdir()
        keep = outside / 'keep.txt'
        keep.write_text('keep')
        link_tag = '2026.9.0'
        (self.bm.versions_dir / link_tag).symlink_to(outside, target_is_directory=True)
        self.bm.remove(link_tag)
        self.assertFalse((self.bm.versions_dir / link_tag).is_symlink())
        self.assertEqual(keep.read_text(), 'keep')


    def test_remove_reports_whether_the_version_is_gone(self):
        final = self.install_old()
        with patch('cftm.binary_manager.shutil.rmtree'):
            self.assertFalse(self.bm.remove(self.tag))
        self.assertTrue(self.bm.is_installed(self.tag))
        self.assertTrue(self.bm.remove(self.tag))
        self.assertFalse(final.parent.exists())
        self.assertTrue(self.bm.remove(self.tag))
        self.assertFalse(self.bm.remove('../escape'))


class FormatSizeTests(unittest.TestCase):
    def test_units_roll_over_at_the_displayed_value(self):
        cases = {0: '0 B', 1023: '1023 B', 1023.6: '1.0 KB', 1024: '1.0 KB', 1536: '1.5 KB',
                 1048575: '1.0 MB', 5 * 1024 ** 2: '5.0 MB', 1024 ** 3: '1.0 GB', 2 * 1024 ** 4: '2048.0 GB'}
        for value, text in cases.items():
            with self.subTest(value=value):
                self.assertEqual(format_size(value), text)
