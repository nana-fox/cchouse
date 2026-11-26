# Windows 安装手册（未装 VMware 时：VirtualBox + Debian 13）

本手册只覆盖 **Windows（x64）** 上 **没有安装 VMware Workstation** 的情况。`cchouse up` 检测到 `vmrun` 时仍走 [install-vmware-linux.md](install-vmware-linux.md)。Intel Mac 不走本手册，继续用 VMware Fusion。Apple Silicon 走 [install-tart-linux.md](install-tart-linux.md)。

客机仍是同一份 Debian 13 amd64 黄金镜像。VirtualBox 只提供 NAT，不替换客机系统。

> 桌面增强（`fcitx5` / `tint2` / `cchouse-power`）自 `v2.0.0-preview.2` 镜像起内置。键盘布局由 `GUEST_KEYBOARD` 控制，默认 `us`，可改为 `gb` / `de` / `jp` 等 2–3 位小写 XKB 代码。

---

## `cchouse up` 会做什么

```bash
python3 -I scripts/cchouse.py up --tag cc01
```

Windows 上选择规则：

- 已安装 VMware（能找到 `vmrun`）→ VMware 后端。
- 否则 → 本手册的 VirtualBox 后端。显式指定仍可用 `--vm-backend vmware` 或 `--vm-backend virtualbox`。

VirtualBox 基础包装从 Oracle 官网下载，版本和 SHA-256 写在 `guest-linux/virtualbox-manifest.json`。下载后校验，再静默安装（`ADDLOCAL=VBoxApplication`，不要桌面快捷方式、不要注册文件关联、不要装完自启）。仓库**不分发**这个安装包，也**不安装** Oracle 的附加包（无 USB 2/3、无主机共享目录）。已有 `VBoxManage.exe` 时跳过下载。NAT-only 安装失败不会改走完整安装。

随后的步骤与 VMware 后端对齐，仍是 9 步：导入 OVA → 挂 `seed.iso` 离线首启 → 卸盘并删除明文 → 接通网卡重启 → `verify-linux.sh --fail-closed` → 快照 `cchouse-clean`。

网络只做 NAT：

| 项 | 值 | 原因 |
|---|---|---|
| `VMNET_CIDR` | `10.0.2.0/24` | VirtualBox NAT 网段，写进客机 SSH 白名单 / mihomo 排除 |
| `GUEST_IP` | `127.0.0.1` | 宿主只通过回环端口转发连接，不是客机内网地址 `10.0.2.15` |
| `SSH_PORT` | `2222` | 宿主 `127.0.0.1:2222` → 客机 `22` |
| `GUEST_IF` | `enp0s3` | VirtualBox NAT 网卡在客机里的名字 |
| `ROUTER_IP` | 不改 | 仍是家里的路由器。验收把它当作必须拦截的地址 |

Windows 上 SSH 使用 `C:\Windows\System32\OpenSSH\ssh.exe`（以及同目录的 `ssh-keyscan.exe`）。不要改用 `C:\Program Files\OpenSSH\ssh.exe`。

开启 BIOS 虚拟化。Windows 11 开着 VBS 时 VirtualBox 可能更慢，**不要为了提速关闭 VBS**。缺 VMware 本身不是 `cchouse up` 的 `HUMAN_WAIT`。单独运行 `preflight.py` 且不传 `--vm-backend virtualbox` 时，没有 `vmrun` 仍会报告 `MISSING_VMWARE`。

---

## 虚拟机形态

导入后 `VBoxManage modifyvm` 固定为：BIOS、2 CPU、2048 MB、VMSVGA 128 MB、网卡 1 为 NAT、型号 `82540EM`、剪贴板双向、关闭拖放、关闭声音、关闭 USB。首启时网线断开，验收重启前再接通。不安装 Guest Additions。

首启把 `seed.iso` 挂到 IDE 的 port 1 / device 0。注入结束后卸盘、`closemedium`，并删除 `seed.iso`。删不掉就停止，不带着明文光盘继续。

---

## 验收与交接

自动验收必须是 `verify-linux.sh --fail-closed` 退出码 0。然后由人在客机桌面核对 `check.html` 全绿，并自行登录。Claude Code 的 OAuth 链接只能在客机浏览器里打开。

断网是保护生效。不要放宽客机 `nftables`，不要加 `DIRECT`，也不要改走宿主代理。
