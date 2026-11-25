# 变更记录

## v2.0-preview

- 新增跨平台 Linux 客机模板（`guest-linux/`，Debian 13 + VMware Workstation/Fusion Pro 路线，开发预览）。
- 出口层与客机隔离层解耦：支持 `EXIT_MODE=single`（单节点）与 `EXIT_MODE=chain`（前置 → 固定 ISP），可选 `STRIPE_EXIT_MODE`。
- 新增面向外部 AI Agent 的 6 步状态机手册（`AGENTS.md`）、`scripts/preflight.py(.ps1)`、`scripts/make-seed-iso.py`、`scripts/verify-linux.sh` 与单元测试 `tests/test_v2.py`。
- 保留 Apple Silicon Mac `tart` + macOS 客机路线（`guest/`、`host/`）。
- 完善 v1 macOS 文档与模板：`docs/build.md` 补充 `mihomo` / `tart-guest-agent` 下载与 sha256 核对命令、明确基础镜像版本及本地 vanilla 底包 CoW 复用说明、移除私有目录硬引用；`instance.env.example` 显式声明 `GUEST_OS`/`EXIT_MODE` 并将外置盘 `VM_STORAGE` 默认置空。
- 修正 `scripts/preflight.py` 的 Tart 底包缓存判定：仅在 `cache/OCIs` 或命名含 `vanilla`/`base` 的本地底包下存在真实 `disk.img` 时视为命中缓存，避免 `tart prune` 残留悬空软链或已有成品实例导致低磁盘误放行，并按是否命中底包区分 30–50 GB 警告文案。
- 修复 Linux v2 真机实测发现的两处跨平台缺陷：`guest-linux/nftables.conf` 的 `chain input` 补全 `iifname "cchouse0" meta nfproto ipv4 accept`（修复 `mihomo` `stack: system` 模式下客机 TCP 连接全被 `policy drop` 拦截的 P0 阻断）；`scripts/verify-linux.sh` 补齐 `-F none` 与 `UserKnownHostsFile` 内层引号以兼容含空格路径（如 macOS `Application Support`）。
- 全面升级 Debian 13 黄金镜像桌面体验（Closes #9）：补齐 `fonts-noto-cjk`/`fonts-noto-color-emoji` 解决中文方块乱码，显式安装 `dbus-user-session` 并预装 `fcitx5` 简体拼音（关闭云拼音、由 `firstboot.sh` 按 `GUEST_KEYBOARD` 动态生成 profile，移除 Openbox 默认 `A-space` 冲突热键以兼容 macOS 左 `Shift`/`Option+Space` 与 Windows `Ctrl+Space`），新增 `tint2` 底部任务栏（`TSC`）/系统托盘与柔和背景色解决最小化黑屏，通过无通配符 `cchouse-power` sudoers 保障右键安全关机/重启，关闭 Xorg 10 分钟 DPMS 熄屏，错峰启动窗口消除竞态，以 `lxterminal`（10000 行回滚）替换 `xterm` 并安装 `xclip`，为 `TARGET_HYPERVISOR=tart` 补装 `spice-vdagent`，在 `20-cchouse-dhcp.network` 配置 `ClientIdentifier=mac`，并统一通过 `/etc/chromium.d/cchouse`（`--hide-crash-restore-bubble`）与托管策略关闭默认书签栏和崩溃恢复弹窗。
- 优化 Apple Silicon `tart-linux` 镜像构建与本地复用：`guest-linux/image-builder/build-tart-linux-from-cloud-image.sh` 改拉锁版本的官方压缩云镜像 `.tar.xz`（~288 MB，替代 3.00 GiB 未压缩 `.raw`），支持先校验再原子落盘、自动清理损坏缓存、同卷临时解压与自定义版本/后缀防呆；`scripts/preflight.py` 统一解析 `tart` 包装脚本中的 `TART_HOME` 并在 `host.tart_home` / `host.tart_linux_golden_images` 输出精确匹配的本地黄金镜像列表。
