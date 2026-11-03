"""Desktop entry Exec quoting and installation regressions; no display required."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cftm.desktop_integration import DESKTOP_FILE_NAME, GENERATED_MARK, _quote_exec_arg, install_desktop_file


_VALUE_ESCAPES = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}


def decode(value):
    """按 Desktop Entry 规范依次还原：值转义、Exec 引号转义、%% -> %，返回单个参数。"""
    assert "\n" not in value and "\r" not in value  # 值里不能有字面换行
    out, chars = [], iter(value)
    for ch in chars:
        out.append(_VALUE_ESCAPES[next(chars)] if ch == "\\" else ch)
    value = "".join(out)
    assert value.startswith('"') and value.endswith('"')
    out, chars = [], iter(value[1:-1])
    for ch in chars:
        out.append(next(chars) if ch == "\\" else ch)
    return "".join(out).replace("%%", "%")


class QuoteExecArgTests(unittest.TestCase):
    def test_plain_path_is_left_alone(self):
        self.assertEqual(_quote_exec_arg("/usr/bin/python3"), "/usr/bin/python3")

    def test_percent_is_doubled(self):
        self.assertEqual(_quote_exec_arg("/p%d"), "/p%%d")

    def test_reserved_characters_round_trip(self):
        for arg in ('/a b/c', '/a\\b', '/q"uote', '/d$ollar', '/b`tick', '/a\\"b',
                    '/a %b/c', '/a\nExec=x', '/a\rb', '/a\tb'):
            with self.subTest(arg=arg):
                self.assertEqual(decode(_quote_exec_arg(arg)), arg)

    def test_install_desktop_file_preserves_custom_and_invalid_utf8_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_home = Path(tmp)
            target = data_home / "applications" / DESKTOP_FILE_NAME
            target.parent.mkdir(parents=True)
            victim = data_home / "victim"
            victim.write_text("keep")
            target.with_name(target.name + ".tmp").symlink_to(victim)
            with patch.dict("os.environ", {"XDG_DATA_HOME": str(data_home)}):
                self.assertTrue(install_desktop_file())
                self.assertIn(GENERATED_MARK, target.read_text(encoding="utf-8"))
                self.assertEqual(victim.read_text(), "keep")

                for raw in (b"\xff\xfe", b"", b"[Desktop Entry]\nExec=custom\n"):
                    target.write_bytes(raw)
                    self.assertFalse(install_desktop_file())
                    self.assertEqual(target.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
