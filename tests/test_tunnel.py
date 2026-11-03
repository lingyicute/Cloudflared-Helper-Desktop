"""Tunnel model and real Gio subprocess lifecycle regressions."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from gi.repository import GLib
from cftm.config import Config
from cftm.tunnel import STATE_ERROR, STATE_RUNNING, STATE_STARTING, STATE_STOPPED, TunnelConfig, TunnelManager, TunnelProcess


def wait_until(predicate, timeout=4):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if context.pending():
            context.iteration(False)
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = TunnelManager(Config(Path(self.tmp.name) / 'config.json'))
        self.procs = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for proc in self.procs:
            proc.stop(force=True)
        wait_until(lambda: all(not p.is_active() for p in self.procs))

    def script(self, body):
        path = Path(self.tmp.name) / 'fake-cloudflared'
        path.write_text('#!/usr/bin/python3\n' + body)
        path.chmod(0o755)
        return str(path)

    def proc(self):
        proc = self.manager.add(TunnelConfig(hostname='tcp.example.com'))
        self.procs.append(proc)
        return proc

    def test_ipv6_listener(self):
        for host in ('::1', '[::1]', '0:0:0:0:0:0:0:1'):
            cfg = TunnelConfig(hostname='tcp.example.com', listen_host=host, port=2222)
            with self.subTest(host=host):
                self.assertEqual(cfg.build_argv('cloudflared')[-1], f'[{host.strip("[]")}]:2222')
                self.assertIn(f'[{host.strip("[]")}]:2222', cfg.command_string())

    def test_listen_label_and_fallback_command(self):
        cases = [('::1', '[::1]:2222'), ('', '127.0.0.1:2222'), ('localhost', 'localhost:2222'),
                 ('http://x', 'http://x:2222')]
        for host, label in cases:
            cfg = TunnelConfig(hostname='tcp.example.com', listen_host=host, port=2222)
            with self.subTest(host=host):
                self.assertEqual(cfg.listen_label, label)
        bad = TunnelConfig(hostname='a b', listen_host='::1', port=2222, extra_args='   ')
        self.assertTrue(bad.command_string().endswith("--url '[::1]:2222'"))
        bad_with_extra = TunnelConfig(hostname='a b', listen_host='::1', port=2222,
                                      extra_args='--no-autoupdate --loglevel debug')
        self.assertTrue(bad_with_extra.command_string().endswith("--url '[::1]:2222' --no-autoupdate --loglevel debug"))

    def test_from_dict_rejects_boolean_port_and_normalizes_fields(self):
        cfg = TunnelConfig.from_dict({'id': '  abc  ', 'port': True, 'listen_host': '   '})
        self.assertEqual(cfg.id, 'abc')
        self.assertEqual(cfg.port, 21128)
        self.assertEqual(cfg.listen_host, '127.0.0.1')

    def test_invalid_arguments_are_rejected_before_spawn(self):
        for field, value in [('hostname', 'tcp\texample.com'), ('hostname', 'tcp\x00.example.com'),
                             ('listen_host', 'http://localhost'), ('listen_host', '[::1'),
                             ('listen_host', 'localhost:8080'), ('extra_args', '--token a\x00b')]:
            cfg = TunnelConfig(hostname='tcp.example.com')
            setattr(cfg, field, value)
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    cfg.build_argv('cloudflared')
                proc = TunnelProcess(cfg)
                with patch('cftm.tunnel.Gio.SubprocessLauncher.new') as launcher:
                    proc.start('cloudflared')
                launcher.assert_not_called()
                self.assertEqual(proc.state, STATE_ERROR)

    def test_deleted_process_is_still_killed_at_shutdown(self):
        proc = self.proc()
        proc.start(self.script('import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint("READY", flush=True)\ntime.sleep(60)\n'))
        self.assertTrue(wait_until(lambda: any('READY' in line for line in proc.log_lines())))
        child = proc._proc
        pid = int(child.get_identifier())
        self.manager.remove(proc.config.id)
        self.assertIsNone(self.manager.get(proc.config.id))
        self.manager.stop_all(force=True)
        self.assertTrue(wait_until(lambda: not proc.is_active(), timeout=1.5))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(self.manager._retiring)

    def test_retiring_process_keeps_port_reserved(self):
        proc = self.proc()
        proc.start(self.script('import time\nprint("READY", flush=True)\ntime.sleep(60)\n'))
        self.assertTrue(wait_until(lambda: any('READY' in line for line in proc.log_lines())))
        self.manager.remove(proc.config.id)
        self.assertIs(self.manager.port_conflict(TunnelConfig(hostname='other.example.com')), proc)
        self.assertTrue(wait_until(lambda: not proc.is_active()))
        self.assertFalse(self.manager._retiring)

    def test_bracketed_ipv6_conflict(self):
        proc = self.proc()
        proc.config.listen_host = '::1'
        proc.state = STATE_RUNNING
        self.assertIs(self.manager.port_conflict(TunnelConfig(listen_host='[::1]')), proc)
        proc.state = STATE_STOPPED

    def test_invalid_utf8_does_not_stop_log_reader(self):
        proc = self.proc()
        proc.start(self.script('import os\nos.write(1, b"bad\\xff\\nlast error\\n")\nraise SystemExit(7)\n'))
        self.assertTrue(wait_until(lambda: proc.state == STATE_ERROR))
        self.assertTrue(any('bad\ufffd' in line for line in proc.log_lines()))
        self.assertTrue(any('last error' in line for line in proc.log_lines()))

    def test_stale_promotion_does_not_change_new_process(self):
        proc = self.proc()
        old, new = object(), object()
        proc._proc, proc.state = new, STATE_STARTING
        proc._promote_running(old)
        self.assertEqual(proc.state, STATE_STARTING)
        proc._proc, proc.state = None, STATE_STOPPED

    def test_listener_log_after_stop_does_not_promote(self):
        proc = self.proc()
        proc.state, proc._stopping = STATE_STARTING, True
        stream = Mock()
        stream.read_bytes_finish.return_value = GLib.Bytes.new(b'listening on 127.0.0.1:2222\n')
        proc._stream = stream
        with patch.object(proc, '_read_next'):
            proc._on_read(stream, None, None)
        self.assertEqual(proc.state, STATE_STARTING)
        proc.state, proc._stream = STATE_STOPPED, None

    def test_eof_finishes_without_busy_reading_or_fallback(self):
        proc = self.proc()
        with patch.object(proc, '_read_next', wraps=proc._read_next) as read:
            proc.start(self.script('import os\nos.write(1, b"first\\n\\nlast")\n'))
            self.assertTrue(wait_until(lambda: not proc.is_active(), timeout=1))
            self.assertLess(read.call_count, 10)
        self.assertTrue(any(line.endswith('first') for line in proc.log_lines()))
        self.assertTrue(any(line.endswith('last') for line in proc.log_lines()))

    def test_long_unterminated_log_line_is_bounded(self):
        proc = self.proc()
        proc.start(self.script('import os\nos.write(1, b"x" * (1024 * 1024) + b"\\nlast\\n")\n'))
        self.assertTrue(wait_until(lambda: not proc.is_active()))
        self.assertLess(max(map(len, proc.log_lines())), 70000)
        self.assertTrue(any(line.endswith('last') for line in proc.log_lines()))

    def test_utf8_split_across_reads_is_preserved(self):
        proc = self.proc()
        proc.start(self.script('import os,time\nos.write(1, b"\\xe4")\ntime.sleep(0.1)\nos.write(1, b"\\xb8\\xad\\n")\n'))
        self.assertTrue(wait_until(lambda: not proc.is_active()))
        self.assertTrue(any(line.endswith('中') for line in proc.log_lines()))

    def test_old_read_does_not_contaminate_new_process(self):
        proc = self.proc()
        stream = Mock()
        stream.read_bytes_finish.return_value = GLib.Bytes.new(b'old process\n')
        proc._stream = Mock()
        proc._line_buffer.extend(b'new process')
        proc._on_read(stream, None, None)
        self.assertEqual(proc._line_buffer, b'new process')
        self.assertEqual(proc.log_lines(), [])
        proc._stream = None

    def test_exited_process_is_not_promoted_while_draining_logs(self):
        proc = self.proc()
        proc._proc = object()
        proc.state = STATE_STARTING
        proc._pending_exit = 1
        proc._promote_running(proc._proc)
        self.assertEqual(proc.state, STATE_STARTING)
        proc._proc, proc.state = None, STATE_STOPPED

    def test_repeated_stop_signals_the_child_once(self):
        proc = self.proc()
        child = Mock()
        proc._proc, proc.state = child, STATE_RUNNING
        try:
            with patch('cftm.tunnel.GLib.timeout_add_seconds') as timer:
                proc.stop()
                proc.stop()
                self.assertEqual(sum('正在断开' in line for line in proc.log_lines()), 1)
                proc.stop(force=True)
            child.send_signal.assert_called_once()
            child.force_exit.assert_called_once()
            timer.assert_called_once()
        finally:
            proc._proc, proc.state, proc._stopping = None, STATE_STOPPED, False

    def test_stop_after_exit_does_not_hide_the_crash(self):
        proc = self.proc()
        child = Mock()
        proc._proc, proc.state, proc._pending_exit = child, STATE_RUNNING, 3
        proc.stop()
        child.send_signal.assert_not_called()
        self.assertFalse(proc._stopping)
        proc._finish_exit()
        self.assertEqual(proc.state, STATE_ERROR)

    def test_retiring_process_blocks_same_id_conflict_and_reuses_fresh_id(self):
        proc = self.proc()
        old_id = proc.config.id
        proc.state = STATE_RUNNING
        self.manager.procs.remove(proc)
        self.manager._retiring.add(proc)
        try:
            same_id_cfg = TunnelConfig(id=old_id, hostname='tcp.example.com', port=proc.config.port)
            self.assertIs(self.manager.port_conflict(same_id_cfg), proc)
            added = self.manager.add(same_id_cfg)
            self.assertNotEqual(added.config.id, old_id)
        finally:
            self.manager._retiring.discard(proc)
            proc.state = STATE_STOPPED

    def test_finish_exit_cancels_promote_and_exit_timers(self):
        proc = self.proc()
        proc._promote_timer = GLib.timeout_add(60000, lambda: False)
        proc._exit_timer = GLib.timeout_add(60000, lambda: False)
        proc.state = STATE_STARTING
        proc._finish_exit(0)
        self.assertIsNone(proc._promote_timer)
        self.assertIsNone(proc._exit_timer)

    def test_fatal_and_usage_logs_update_last_error(self):
        for line in ('2026-01-01T00:00:00Z FTL failed to bind',
                     'Incorrect Usage: flag provided but not defined: -foo'):
            proc = self.proc()
            proc._line_buffer.extend(line.encode('utf-8'))
            proc._flush_log_line()
            self.assertEqual(proc.last_error, line)

    def test_repeated_invalid_start_still_emits_error_signal(self):
        proc = TunnelProcess(TunnelConfig(hostname='bad host'))
        states = []
        proc.connect('state-changed', lambda _p, st: states.append(st))
        proc.start('cloudflared')
        proc.start('cloudflared')
        self.assertEqual(states, [STATE_ERROR, STATE_ERROR])

    def test_on_exit_cancels_kill_and_promote_timers_before_eof(self):
        proc = self.proc()
        child = Mock()
        child.get_if_exited.return_value = True
        child.get_exit_status.return_value = 0
        proc._proc, proc.state = child, STATE_STARTING
        proc._stream, proc._eof = object(), False
        proc._kill_timer = GLib.timeout_add(60000, lambda: False)
        proc._promote_timer = GLib.timeout_add(60000, lambda: False)
        proc._on_exit(child, None)
        self.assertIsNone(proc._kill_timer)
        self.assertIsNone(proc._promote_timer)
        self.assertEqual(proc._pending_exit, 0)
        proc._force_kill(child)
        child.force_exit.assert_not_called()
        proc._finish_exit()

    def test_listener_and_conflict_edge_cases(self):
        cfg = TunnelConfig(hostname='tcp.example.com', port=True)
        with self.assertRaises(ValueError):
            cfg.build_argv('cloudflared')
        bad = TunnelConfig(hostname=None, listen_host=None, port=2222)
        self.assertEqual(bad.listen_label, '127.0.0.1:2222')
        self.assertIn("127.0.0.1:2222", bad.command_string())

        proc = self.proc()
        proc.config.listen_host = '::ffff:127.0.0.1'
        proc.state = STATE_RUNNING
        self.assertIs(self.manager.port_conflict(TunnelConfig(listen_host='localhost.')), proc)
        proc.state = STATE_STOPPED
