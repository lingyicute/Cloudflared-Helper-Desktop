#!/usr/bin/env python3
"""快速隧道链接识别 / 默认命名的纯逻辑测试。

不需要显示环境，直接跑：

    python3 tests/test_quick_tunnel.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cftm.quick_tunnel import parse_quick_tunnel_url, quick_tunnel_name  # noqa: E402

# (剪贴板内容, 应当识别出的主机名)
SHOULD_MATCH = [
    ("foo-bar.trycloudflare.com", "foo-bar.trycloudflare.com"),
    ("  https://Foo-Bar.trycloudflare.com  ", "foo-bar.trycloudflare.com"),
    ("https://x.trycloudflare.com/", "x.trycloudflare.com"),
    ("https://x.trycloudflare.com/some/path?a=1&b=2", "x.trycloudflare.com"),
    ("http://a.trycloudflare.com", "a.trycloudflare.com"),
    ("https://x.trycloudflare.com:8443", "x.trycloudflare.com"),
    ("<https://x.trycloudflare.com>", "x.trycloudflare.com"),
    ("'https://x.trycloudflare.com'", "x.trycloudflare.com"),
    ("“https://x.trycloudflare.com”", "x.trycloudflare.com"),
    ("(https://x.trycloudflare.com)", "x.trycloudflare.com"),
    ("a1.trycloudflare.com\n", "a1.trycloudflare.com"),
]

# 这些一律不该弹窗
SHOULD_REJECT = [
    None,
    "",
    "   ",
    "hello world",
    "trycloudflare.com",
    "https://trycloudflare.com",
    "https://x.trycloudflare.com.evil.com",      # 后缀伪装：真正的域名是 evil.com
    "evil.com/x.trycloudflare.com",
    "visit https://x.trycloudflare.com now",     # 一大段文本里不搜链接
    "https://x.example.com",
    "ssh://x.trycloudflare.com",
    "https://.trycloudflare.com",
    "https://x.trycloudflare.com and https://y.trycloudflare.com",
    "file:///home/user/x.trycloudflare.com",
    "https://-x.trycloudflare.com",
]


def main() -> int:
    failures = 0
    for text, want in SHOULD_MATCH:
        got = parse_quick_tunnel_url(text)
        if got != want:
            failures += 1
            print(f"FAIL  match  {text!r} -> {got!r} (期望 {want!r})")
    for text in SHOULD_REJECT:
        got = parse_quick_tunnel_url(text)
        if got is not None:
            failures += 1
            print(f"FAIL  reject {text!r} -> {got!r} (期望 None)")

    # 名称格式：yyyymmdd hh:mm:ss 快速隧道
    when = time.mktime(time.strptime("20260915 09:08:07", "%Y%m%d %H:%M:%S"))
    name = quick_tunnel_name(when)
    if name != "20260915 09:08:07 快速隧道":
        failures += 1
        print(f"FAIL  name   -> {name!r}")
    if not quick_tunnel_name().endswith(" 快速隧道"):
        failures += 1
        print("FAIL  name   -> 不传时间戳时应当用当前时间")

    total = len(SHOULD_MATCH) + len(SHOULD_REJECT) + 2
    print(f"test_quick_tunnel: {total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
