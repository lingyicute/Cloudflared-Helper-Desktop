"""Desktop entry Exec quoting; no display required."""
import unittest

from cftm.desktop_integration import _quote_exec_arg


def decode(value):
    """按 Desktop Entry 规范先还原值转义，再还原 Exec 引号转义，返回单个参数。"""
    value = value.replace("\\\\", "\\")  # 值层面的 \\ -> \
    assert value.startswith('"') and value.endswith('"')
    out, chars = [], iter(value[1:-1])
    for ch in chars:
        out.append(next(chars) if ch == "\\" else ch)
    return "".join(out)


class QuoteExecArgTests(unittest.TestCase):
    def test_plain_path_is_left_alone(self):
        self.assertEqual(_quote_exec_arg("/usr/bin/python3"), "/usr/bin/python3")

    def test_percent_is_doubled(self):
        self.assertEqual(_quote_exec_arg("/p%d"), "/p%%d")

    def test_reserved_characters_round_trip(self):
        for arg in ('/a b/c', '/a\\b', '/q"uote', '/d$ollar', '/b`tick', '/a\\"b'):
            with self.subTest(arg=arg):
                self.assertEqual(decode(_quote_exec_arg(arg)), arg)


if __name__ == "__main__":
    unittest.main()
