"""``python3 -m cftm`` 入口，与 desktop_integration 里 .desktop 的回退 Exec 对应。"""
import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
