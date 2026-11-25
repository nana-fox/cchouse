# Apple Silicon Mac 轻量安装手册（Tart + Debian 13 arm64）

本手册面向 **Apple Silicon Mac（M1/M2/M3/M4）**，采用 `tart` + 预装好的 **Debian 13 (`arm64`) 黄金镜像**（压缩包 ~725 MB，磁盘占用 ~3 GB），支持 `EXIT_MODE=single`（单节点）与 `chain`（链式），仅覆盖网页版与 Claude Code CLI（不含 Claude Desktop）。

> 若需要 macOS 桌面与 Safari 高拟真环境，请改走 [build.md](build.md)（v1 `Tart + macOS`）。

---

## 阶段 0：前提准备

1. 安装 `tart`：`brew install cirruslabs/cli/tart`（或将官方 release 放外置盘并配置 `TART_HOME`）。
2. 自备外部 IP 代理，按 [proxy-modes.md](proxy-modes.md) 确定 `EXIT_MODE=single` 或 `chain`。

---

## 阶段 1：环境自检、拉取 Claude Code 与渲染配置

```bash
python3 -I scripts/preflight.py --route tart-linux
```

在私有目录（默认 `~/Library/Application Support/cchouse/<TAG>`，权限 `700`）生成 SSH 密钥并准备 `instance.env`（复制 `instance.env.linux.example`）：

- `GUEST_OS=linux`
- `VMNET_CIDR=192.168.64.0/24`（Tart 默认共享 NAT 网段）
- `SSH_PUBKEY` 填入刚生成的 `id_ed25519.pub` 单行内容

拉取校验后的 Claude Code 原生 `linux-arm64` 包并渲染：

```bash
python3 -I scripts/fetch-claude-code.py --platform linux-arm64 --out "<PRIVATE_DIR>/claude-code.tgz"
python3 -I scripts/preflight.py --route tart-linux --env "<PRIVATE_DIR>/instance.env"
python3 -I scripts/render.py "<PRIVATE_DIR>/instance.env" "<PRIVATE_DIR>/rendered"
python3 -I scripts/make-seed-iso.py "<PRIVATE_DIR>/rendered" "<PRIVATE_DIR>/seed.iso" --claude-tarball "<PRIVATE_DIR>/claude-code.tgz"
```

---

## 阶段 2：导入黄金镜像并克隆实例

从 `guest-linux/image-manifest.json` 的 `arm64 / tart-linux` 条目下载 `cchouse-debian13-arm64.tvm` 并核对 `sha256`：

```bash
shasum -a 256 cchouse-debian13-arm64.tvm
tart import cchouse-debian13-arm64 cchouse-debian13-arm64.tvm
tart clone cchouse-debian13-arm64 "<VM_NAME>"
```

---

## 阶段 3：首次离线注入与清理

1. **物理断网首次开机注入**：
   - 使用 `--disk "<PRIVATE_DIR>/seed.iso:ro"` 启动客机（镜像内置 `cchouse-default-drop.service` 会在 `network-pre.target` 前拦截全部非回环流量；若需物理层零出包，可通过 `--net-fd` 绑定死 socketpair）。
   - 等待约 30–40 秒待 `firstboot` 完成（失败时可看控制台 `/etc/issue.d/cchouse.issue` 或 `/var/lib/cchouse/firstboot.log`）。
2. **关机、卸载并销毁明文**：
   - 关机后**立即删除 `<PRIVATE_DIR>/seed.iso` 与 `<PRIVATE_DIR>/rendered/`**。
   - **后续启动严禁再带 `--disk seed.iso`**，防止明文凭据长期挂在客机内。
3. **正常开机并获取 IP**：
   ```bash
   tart run "<VM_NAME>" &
   # 黄金镜像（preview.2 起）已配置 ClientIdentifier=mac，优先默认 DHCP 解析；若未命中可查看 /var/db/dhcpd_leases 顶部
   tart ip "<VM_NAME>" --wait 30 || head -n 30 /var/db/dhcpd_leases
   ```
   将拿到的 IP 写入 `instance.env` 的 `GUEST_IP`，在客机桌面 `lxterminal` 执行 `cat /etc/ssh/ssh_host_ed25519_key.pub` 核对主机公钥并写入 `<PRIVATE_DIR>/known_hosts`。

---

## 阶段 4：自动验收与干净快照

```bash
scripts/verify-linux.sh "<PRIVATE_DIR>/instance.env" --fail-closed
```

退出码为 `0` 后关机并打干净快照：

```bash
tart clone "<VM_NAME>" "<VM_NAME>-clean-$(date +%Y%m%d)"
```

随后打开客机桌面 → Chromium 打开 `file:///usr/local/<TAG>/check.html` 核对全绿，再登录 `claude.ai` 或客机终端执行 `claude`（**OAuth 链接只能在客机 Chromium 内打开，严禁复制到宿主浏览器**）。

> **桌面操作提示（`v2.0.0-preview.2` 起）**：黄金镜像内置思源黑体与 `fcitx5` 简体拼音，默认英文键盘，**轻按左 `Shift`** 或 **`Ctrl + Space`** 一键切换中英文；底部 `tint2` 任务栏可还原最小化窗口并查看右下角输入法图标与出口时间；桌面右键菜单提供浏览器、终端与安全关机入口。
