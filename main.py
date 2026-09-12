#!/usr/bin/env python3
"""Cloudflared 隧道连接管理器 — 程序入口"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _self_test() -> int:
    """无需显示器即可验证运行环境：GTK4 / libadwaita 的 typelib 是否齐全、版本是否正确。

    供 CI 在 PyInstaller 打包后调用（``cloudflared-tunnel-manager --self-test``）。
    若 typelib 没有被正确收集，这里会复现 "Namespace Gtk not available" 并以非零码退出。
    """
    import gi

    try:
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        gi.require_version("Pango", "1.0")
        from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: F401
    except (ValueError, ImportError) as exc:
        print(f"self-test FAILED: {exc}", file=sys.stderr)
        print(f"GI_TYPELIB_PATH={os.environ.get('GI_TYPELIB_PATH', '')}", file=sys.stderr)
        return 1

    print(
        "self-test OK: "
        f"Gtk {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}, "
        f"Adw {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}, "
        f"frozen={getattr(sys, 'frozen', False)}"
    )
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())

    from cftm.app import main  # noqa: E402

    sys.exit(main(sys.argv))
