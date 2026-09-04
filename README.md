# Cloudflared-Helper-Desktop —— Cloudflared 隧道管理器

一个基于 **Python + GTK4 + libadwaita** 的现代化桌面应用，用于管理多条
[Cloudflare Tunnel](https://www.cloudflare.com/products/tunnel/) 客户端连接
（`cloudflared access tcp/ssh/rdp/smb`），派生自
[lingyicute/Cloudflared-Helper](https://github.com/lingyicute/Cloudflared-Helper)。

## 功能

- **多隧道并发**：保存任意数量的隧道，可同时连接多条；每条隧道独立的状态指示、启停按钮与实时日志。
- **cloudflared 版本管理**
  - 自动识别系统/架构（`amd64` / `arm64` / `arm` / `386`，Linux 与 macOS）
  - 从 GitHub Releases 拉取版本列表，一键下载安装，显示下载进度，可取消
  - 多版本并存，随时在“最新已安装 / 系统 PATH / 指定版本”之间切换
  - 自动 `chmod +x`，探测并展示当前生效的版本
- **智能校验**：端口范围检查、低于 1024 端口的权限提示、本地端口冲突检测。
- **贴心细节**：程序启动时自动连接、复制等效命令行、失败时的日志快捷跳转、退出前的断开确认。
- 配置保存在 `~/.config/cloudflared-tunnel-manager/config.json`，
  二进制保存在 `~/.local/share/cloudflared-tunnel-manager/versions/<版本>/`。

## 运行

依赖：Python ≥ 3.10、GTK 4、libadwaita ≥ 1.4、PyGObject。

```bash
# Debian / Ubuntu 24.04+
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
# Fedora
sudo dnf install python3-gobject gtk4 libadwaita
# Arch
sudo pacman -S python-gobject gtk4 libadwaita

python3 main.py
```

## 构建（GitHub Actions）

`.github/workflows/build.yml` 包含四个作业：

| 作业 | 说明 |
| --- | --- |
| `check` | 字节编译 + 在 Xvfb 中导入全部模块，验证 GTK/Adw API |
| `pyinstaller` | 在 `ubuntu-24.04` / `ubuntu-24.04-arm` 上打包便携 tar.gz（x86_64、aarch64） |
| `flatpak` | 使用 GNOME 47 运行时构建 `.flatpak` 包（推荐分发方式） |
| `release` | 推送 `v*` 标签时自动创建 GitHub Release 并上传所有产物 |

本地构建 Flatpak：

```bash
flatpak-builder --user --install --force-clean build-dir \
  flatpak/uk.92li.cftm.CloudflaredTunnelManager.json
flatpak run uk.92li.cftm.CloudflaredTunnelManager
```

## 目录结构

```
main.py                      入口
cftm/app.py                  Adw.Application、主窗口、隧道列表
cftm/tunnel.py               隧道模型 + Gio.Subprocess 进程管理
cftm/binary_manager.py       cloudflared 版本管理
cftm/versions_page.py        版本管理页面
cftm/dialogs.py              编辑对话框、日志窗口
cftm/config.py               JSON 配置
data/                        desktop / metainfo / 图标
flatpak/                     Flatpak 清单
.github/workflows/build.yml  CI / 发布流程
index.html                   项目主页
```

## 许可证

AGPL-3.0-or-later
