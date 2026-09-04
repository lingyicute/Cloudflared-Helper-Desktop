#!/usr/bin/env python3
"""Cloudflared 隧道连接管理器 — 程序入口"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cftm.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv))
