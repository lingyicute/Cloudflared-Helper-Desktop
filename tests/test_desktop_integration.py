"""Desktop entry Exec quoting; no display required."""
import unittest

from cftm.desktop_integration import _quote_exec_arg


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


if __name__ == "__main__":
    unittest.main()
