# 变更记录

## [v2.0.0-preview.5](https://github.com/nana-fox/cchouse/releases/tag/v2.0.0-preview.5) · 2026-11-27

- 新增日常命令 `open`、`stop`、`status`、`restore`。安装时写入命令入口（Mac `~/.local/bin/cchouse`，Windows `%LOCALAPPDATA%\cchouse\bin\cchouse.cmd`），并在桌面放启动图标（Mac `~/Applications` 应用加桌面替身，Windows 桌面与开始菜单快捷方式）。`remove` 一并清理图标。
- `stop` 经 SSH 让客机正常关机，强停需 `--force`。`restore` 安全停机后回到干净快照，保留节点配置与 SSH 密钥。
- Windows：点虚拟机窗口关闭按钮即正常关机，不再弹出断电或保存状态选项。装完改为离线干净快照并以窗口交给用户；经 SSH 等非交互会话安装时提示双击图标打开。
- Mac：点 Tart 窗口关闭按钮等于断电，`open` 会提示改用客机右键安全关机或 `cchouse stop`。私有 Tart 可被后续命令找到。
- 引导下载断点续传、显示进度、多次退避重试，遇到 HTTP/2 断流改用 HTTP/1.1，失败时说明是哪一段并给出下一步。Windows 引导改用系统 curl，新增 `-Tag`、`-ImageMirror`、`-NpmMirror`；Mac 引导支持 `--image-mirror`、`--npm-mirror`。
- 大文件下载有进展时不计入失败次数，长时间低速自动断开续传，npm 元数据请求加重试。
- 两个平台继续使用 preview.2 的公开镜像。

## [v2.0.0-preview.4](https://github.com/nana-fox/cchouse/releases/tag/v2.0.0-preview.4) · 2026-11-26

- 中英首页增加 Apple Silicon Mac 的 SHA-256 锁定引导入口。Mac 引导支持 `--tag`，续跑核对实例归属，遇到同名的外部虚拟机或快照时停止并提示换名。
- 快速安装接受 SOCKS5 和 HTTP(S)，通过节点读取实际出口 IPv4、国家或地区、ASN 与时区。VLESS、Trojan、Shadowsocks 指向进阶配置。节点输入期间暂停后台下载日志。
- `VERSION` 统一 CLI、引导和打包版本。诊断包记录安装器版本、已导入镜像身份和可读取的客机构建信息。
- amd64 和 arm64 继续使用 preview.2 的公开镜像。 Windows 仍在离线首启收到完成信号并确认客机关机后才卸盘联网。
- Windows 自动验收失败时，`cchouse up` 保留验收退出码。GTR 验收使用不分配 PTY 的 SSH 连接，避免 Windows OpenSSH 将失败报告为退出码 0。
- Windows 宿主出口探测超时时，通过原节点限次重试。安装继续前仍需取得四项出口信息。

## [v2.0.0-preview.3](https://github.com/nana-fox/cchouse/releases/tag/v2.0.0-preview.3) · 2026-11-26

- 发布锁定的源码 ZIP、Mac/Windows 引导脚本与 SHA-256 清单。Windows 提供单行 PowerShell 安装，缺少 VirtualBox 时校验后安装 Oracle VirtualBox 7.2.22 基础包。
- Windows 安装改为 VirtualBox + NAT，移除 VMware 安装后端。Windows 和 Mac 加入按安装记录预览、确认、卸载的流程。
- VirtualBox 经本地端口转发和 SSH 执行首启、注入与验收。首次主机密钥优先从串口读取，Windows 验收使用系统 OpenSSH。
- 发布版沿用 preview.2 的 amd64 OVA 和 arm64 TVM。

## [v2.0.0-preview.2](https://github.com/nana-fox/cchouse/releases/tag/v2.0.0-preview.2) · 2026-11-26

- 发布 Debian 13 amd64 OVA（当时面向 VMware Workstation）和 Apple Silicon Tart arm64 TVM。镜像加入 CJK 字体、fcitx5、tint2、Chromium 桌面策略、受限的关机/重启授权与 Tart 剪贴板支持。
- 引入 `up` 交互安装。后台并行下载、摘要校验、断点续传、计时、失败清理与续跑，自动离线注入、验收并保存干净快照。
- 加入脱敏诊断、Stripe 可达性 WARN 和可选专用 Stripe 节点。镜像写入构建提交与时间，首启离线安装 Claude Code 时原子落盘。
- 修复 VMware OVA 导入、Tart 私有路径空格、macOS bash 3.2 验收、下载提前 EOF 和构建镜像源残留。

## [v2.0.0-preview.1](https://github.com/nana-fox/cchouse/releases/tag/v2.0.0-preview.1) · 2026-11-25

- 首次发布空状态 Debian 13 黄金镜像。amd64 为 VMware Workstation/Fusion OVA，Apple Silicon 为 Tart arm64 TVM。两个制品随附 SHA-256，均通过 17 项空状态检查。
- Linux 客机模板提供单节点与链式代理、默认断网防护和离线首启注入。Claude Code 由宿主校验后离线安装。
