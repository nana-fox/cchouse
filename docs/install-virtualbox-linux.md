# Windows 安装手册（VirtualBox + Debian 13）

本手册覆盖 **Windows（x64）** 上 `cchouse up` 的 VirtualBox 路线。已有旧版虚拟机实例时，为 VirtualBox 使用新的 tag。Apple Silicon 走 [install-tart-linux.md](install-tart-linux.md)。

客机仍是同一份 Debian 13 amd64 黄金镜像。VirtualBox 只提供 NAT，不替换客机系统。

> 桌面增强（`fcitx5` / `tint2` / `cchouse-power`）自 `v2.0.0-preview.2` 镜像起内置。键盘布局由 `GUEST_KEYBOARD` 控制，默认 `us`，可改为 `gb` / `de` / `jp` 等 2–3 位小写 XKB 代码。

---

## `cchouse up` 会做什么

```powershell
py -3 -I scripts/cchouse.py up --tag cc01
```

Windows 安装器使用 VirtualBox。单独自检可运行 `py -3 -I scripts/preflight.py --route virtualbox-linux`。粘贴节点链接后，安装器会自动采用并显示出口时区和预期出口 IP，然后继续安装。

缺少 `VBoxManage.exe` 时，`up` 从 Oracle 官网下载 VirtualBox 7.2.22 基础包，按清单中的官方 SHA-256 校验。安装前会显示说明，随后以 NAT 所需的最小组件静默安装。普通权限下只需确认一次 Windows UAC。下载、校验或安装失败时，`up` 会停止并显示 Oracle 官方安装包链接。已有 VirtualBox 时直接复用。

安装器随后执行：导入 OVA → 挂 `seed.iso` 离线首启 → 卸盘并删除明文 → 接通网卡重启 → `verify-linux.sh --fail-closed` → 快照 `cchouse-clean`。

网络只做 NAT：

| 项 | 值 | 原因 |
|---|---|---|
| `VMNET_CIDR` | `10.0.2.0/24` | VirtualBox NAT 网段，写进客机 SSH 白名单 / mihomo 排除 |
| `GUEST_IP` | `127.0.0.1` | 宿主只通过回环端口转发连接，不是客机内网地址 `10.0.2.15` |
| `SSH_PORT` | `2222`–`2421` 中按 tag 选一个空闲端口 | 宿主只绑定 `127.0.0.1:<SSH_PORT>` → 客机 `22`。同一 tag 若 `instance.env` 里已有仍空闲的端口则沿用。探测 SSH 前会核对这台虚拟机的转发规则和监听进程 |
| `GUEST_IF` | `enp0s3` | VirtualBox NAT 网卡在客机里的名字 |
| `ROUTER_IP` | 不改 | 仍是家里的路由器。验收把它当作必须拦截的地址 |

Windows 上 SSH 使用 `C:\Windows\System32\OpenSSH\ssh.exe`。`C:\Program Files\OpenSSH\ssh.exe` 会忽略 `ConnectTimeout`。`ssh-keyscan.exe` 在开启 HVCI 时会卡住，这条路径不再调用它。首启时客机把 ed25519 主机公钥写到串口日志 `logs/hostkey.serial`，宿主用这一行写入 `known_hosts`。日志里没有这一行时，先确认 `127.0.0.1:<SSH_PORT>` 的 NAT 转发规则和监听进程都属于这台虚拟机，再用 `ssh.exe -o StrictHostKeyChecking=accept-new` 做第一次连接。这种兜底会在 `verify-linux.sh` 里记一条 WARN：未经串口核对主机密钥。SSH 就绪最多等待 180 秒，单次探测超过 10 秒会终止。主机密钥对不上就停止。

开启 BIOS 虚拟化。Windows 11 开着 VBS 时 VirtualBox 可能更慢，**不要为了提速关闭 VBS**。

---

## 虚拟机形态

导入后 `VBoxManage modifyvm` 固定为：BIOS、2 CPU、2048 MB、VMSVGA 128 MB、网卡 1 为 NAT、型号 `82540EM`、剪贴板双向、关闭拖放、关闭声音、关闭 USB。首启时网线断开，验收重启前再接通。不挂共享目录。

`up` 下载的已发布版本以 `guest-linux/image-manifest.json` 为准。preview.4 的 Windows 安装沿用 preview.2 amd64 OVA，该镜像没有 VirtualBox Guest Additions。

从源码新构建的 amd64 黄金镜像包含 VirtualBox Guest Additions 7.2.22 和 `open-vm-tools`。构建用的 `VBoxGuestAdditions_7.2.22.iso` 的 SHA-256 取自 Oracle 官方清单，写在 `guest-linux/virtualbox-manifest.json` 的 `guest_additions`。构建只运行这份 GPL 安装器。为当前内核编译模块后，删除 ISO，并 purge `dkms`、`linux-headers` 与 `build-essential`。

镜像在构建时已经 purge `unattended-upgrades`，并禁用 `apt-daily.timer` 与 `apt-daily-upgrade.timer`（`firstboot.sh` 会再关一次）。没有自动升级内核的路径，所以不把编译器留在客机里，也不锁定内核包。以后如果有人手动升级内核，要重做镜像，`vboxguest` 才会重新对上。

服务按 DMI 分开：`open-vm-tools` 只在 VMware 启动（`ConditionVirtualization=vmware`），`vboxadd` 与 `vboxadd-service` 只在 VirtualBox 启动（systemd 的 `oracle`，即 `innotek GmbH` / `VirtualBox`）。Openbox 只在 VirtualBox 下启动 `VBoxClient --clipboard` 和 `VBoxClient --vmsvga`。不启动拖放，不启动无缝模式。宿主侧剪贴板仍是 `--clipboard-mode bidirectional`，拖放仍是 `--drag-and-drop disabled`。Tart / arm64 不装 Guest Additions，仍然只有 `spice-vdagent`。Additions 走虚拟机后门，不改客机 `nftables`。

首启把 `seed.iso` 挂到 IDE 的 port 1 / device 0。客机将配置刷盘后，向串口写入 `CCHOUSE_FIRSTBOOT_DONE` 并正常关机。安装器在 180 秒内确认关机和完成信号后才卸盘、`closemedium`、删除 `seed.iso` 并联网。首启未完成时，排查后可用同一命令重新导入并离线注入。

---

## 验收与交接

自动验收必须是 `verify-linux.sh --fail-closed` 退出码 0。然后由人在客机桌面核对 `check.html` 全绿，并自行登录。Claude Code 的 OAuth 链接只能在客机浏览器里打开。

`cchouse.py remove --tag <tag>` 根据 `state.json` 预览要卸载的实例。确认后核对 VirtualBox 中注册的虚拟机名和实例内的 `.vbox` 路径，再关闭该虚拟机并按它的 UUID 执行 `VBoxManage unregistervm --delete`。核对或关机失败会保留实例记录。

断网是保护生效。不要放宽客机 `nftables`，不要加 `DIRECT`，也不要改走宿主代理。
