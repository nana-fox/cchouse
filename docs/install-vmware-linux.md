# Windows / Intel Mac 安装手册（VMware + Debian 13）

本手册面向 **Windows（x64，家庭版与专业版通用）** 与 **Intel Mac**，采用免费版 VMware + 预装好的 Debian 13 黄金镜像（`guest-linux/`），只覆盖网页版与 Claude Code CLI（不含 Claude Desktop）。

> Apple Silicon Mac 优先走 [build.md](build.md)（Tart + macOS 客机）。

---

## 阶段 0：人工准备（一次性）

以下两项涉及账号登录或系统重启，AI Agent 会停在 `HUMAN_WAIT` 等待用户完成：

1. **开启 CPU 虚拟化**：确认 BIOS/UEFI 已开启 Intel VT-x / AMD-V。
   - *Windows 11 VBS / 内存完整性提示*：若系统开启了 VBS，VMware 17.x 会自动在 WHP 之上运行（略慢但可用，且嵌套虚拟化不可用）。`preflight` 会提示该状态，**不要为了提速关闭 VBS**。
2. **安装免费版 VMware**（Broadcom 自 2024-11 起对个人与商用均免费，需登录 Broadcom 官网下载官方原版安装包并核对签名）：
   - Windows：VMware Workstation Pro
   - Intel Mac：VMware Fusion Pro
3. **自备外部 IP 代理**：按 [proxy-modes.md](proxy-modes.md) 确定 `EXIT_MODE=single` 或 `chain`。

---

## 阶段 1：环境自检与参数准备

```bash
# Windows 无 Python 时先运行：
#   powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1
python3 -I scripts/preflight.py --route vmware-linux
```

确认输出 JSON 中 `ok: true`（已检测到 `vmrun` 路径与足够磁盘空间），再在**私有目录**准备 `instance.env`（不要放进仓库）：

- Windows 示例：`%LOCALAPPDATA%\cchouse\<TAG>\instance.env`
- macOS 示例：`~/Library/Application Support/cchouse/<TAG>/instance.env`

复制 `instance.env.linux.example` 并填写，随后校验与渲染：

```bash
python3 -I scripts/preflight.py --route vmware-linux --env "<PRIVATE_DIR>/instance.env"
python3 -I scripts/render.py "<PRIVATE_DIR>/instance.env" "<PRIVATE_DIR>/rendered"
```

---

## 阶段 2：获取黄金镜像（`.ova`）并导入

黄金镜像为 **Debian 13 (`amd64`)**，预装 Openbox 极简桌面 + Chromium + Node.js + `mihomo` + `nftables` + `open-vm-tools`，**不含任何凭据，且不含 Claude Code（避免非开源包再分发风险）**。

镜像出厂必须满足以下**空状态断言**（防止批量交付撞脸）：
- `/etc/machine-id` 为空文件
- `/etc/ssh/ssh_host_*` 不存在
- 无 Chromium 用户目录、无 `~/.claude*`、无 npm 缓存
- 已卸载/禁用 `avahi-daemon`、`cups-browsed`、NetworkManager 联网探针
- 出厂内置 `guest-linux/nftables-default-drop.conf`（仅放行回环，排在 `network-pre.target` 之前）

下载并校验 `guest-linux/image-manifest.json` 指定的 `.ova` 哈希后导入 VMware（或用 `ovftool` / VMware GUI 导入），记录生成的 `.vmx` 路径。

---

## 阶段 3：首次开机零泄漏注入（三层硬锁）

1. **准备 SSH 密钥并配置 `.vmx`**：
   - 在私有目录生成专用密钥：`ssh-keygen -t ed25519 -f "<PRIVATE_DIR>/id_ed25519" -N ""`，将 `.pub` 单行内容填入 `instance.env` 的 `SSH_PUBKEY`（**必填**，firstboot 会关闭密码登录）。
   - 编辑 `.vmx`，确保网络使用 NAT、首启物理断开、禁用共享文件夹与拖放：
     ```ini
     ethernet0.connectionType = "nat"
     ethernet0.startConnected = "FALSE"
     isolation.tools.hgfs.disable = "TRUE"
     isolation.tools.dnd.disable = "TRUE"
     ```
2. **制作 `CCHOUSE` 卷标配置 ISO**：
   ```bash
   python3 -I scripts/make-seed-iso.py "<PRIVATE_DIR>/rendered" "<PRIVATE_DIR>/seed.iso" [--claude-tarball "<PRIVATE_DIR>/claude-code.tgz"]
   ```
   将 `seed.iso` 挂载到虚拟光驱（确保光驱设置为开机连接）。
   - **不采用 `cloud-init`**，**不采用 VMware `guestinfo.*`**。
3. **首次开机并在控制台确认注入完成（不要用 `nogui` 盲等）**：
   - 启动虚拟机（Workstation 用 `-T ws`，Fusion 用 `-T fusion`），在 VMware 控制台确认：
     - `/var/lib/cchouse/firstboot.done` 已生成；
     - 在控制台查看客机新生成的 SSH 主机公钥（`cat /etc/ssh/ssh_host_ed25519_key.pub`）。
4. **销毁明文、接通网卡并录入 `known_hosts`**：
   - 关机或卸载虚拟光驱，**立即删除 `seed.iso` 与 `<PRIVATE_DIR>/rendered/`**；
   - 将 `.vmx` 改为 `ethernet0.startConnected = "TRUE"` 后启动（或实测 `vmrun -T ws|fusion connectNamedDevice "<VMX_PATH>" ethernet0`）；
   - 在控制台 `ip -4 addr` 确认 NAT DHCP 分配的 `GUEST_IP` 与网段（回填 `instance.env` 的 `GUEST_IP` / `VMNET_CIDR`），并将步骤 3 核对过的 SSH 主机公钥写入 `<PRIVATE_DIR>/known_hosts`。

---

## 阶段 4：自动验收与快照

```bash
scripts/verify-linux.sh "<PRIVATE_DIR>/instance.env" --fail-closed
```

- 仅当退出码为 `0` 时，视为环境验收通过。
- 在**登录任何账号之前**，打一份干净快照：
  ```bash
  vmrun -T ws snapshot "<VMX_PATH>" clean-<YYYYMMDD>
  ```
- 移交用户：打开客机桌面 → Chromium 打开 `file:///usr/local/<TAG>/check.html` 确认全绿 → 登录 `claude.ai` 或在客机终端执行 `claude` 登录（**OAuth 链接只能在客机 Chromium 内打开，严禁复制到宿主浏览器**）。首次登录完成后可另打恢复快照 `recovery-<YYYYMMDD>`（禁止将该快照克隆成多台机器）。

---

## 首次 Windows / Fusion 实机验证清单（定稿前必测）

1. `vmrun connectNamedDevice <vmx> ethernet0` 在 Workstation Pro 与 Fusion Pro 上是否均可用。
2. Linux `mihomo` 开启 TUN（`device: cchouse0`、`auto-redirect: false`）时，与独立 `table inet cchouse_guard`（正向放行 `oifname "cchouse0"`）的共存与重载行为。
3. Win11 开启 VBS / 内存完整性下 VMware 17.x 的启动速度与交互流畅度。
4. VMware 导入同一 `.ova` 两次后，网卡 MAC 与 BIOS UUID 是否自动重新生成。
5. `cchouse-firstboot.service` 在 `network-pre.target` 之前执行时，光驱设备节点（`/dev/disk/by-label/CCHOUSE`）的 udev 就绪时机。
6. Claude Code npm tarball 离线安装（`npm install -g --offline`）在 Debian 13 下的依赖完整性。
