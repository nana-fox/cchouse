# cchouse · AI Agent 执行手册

## AI 带装

面向协助用户在本机安装 cchouse 的 AI 助手（需具备本机命令执行能力，如 Claude Code、Codex、Cursor）。按以下顺序逐步执行：

1. **检查电脑**：确认宿主机平台与可用磁盘空间。仅支持 Apple Silicon Mac（macOS 13+，安装卷至少 9 GB / 建议 12 GB）和 Windows x64（安装卷至少 7 GB / 建议 10 GB）。Intel Mac、Linux 宿主或其他平台直接告知暂不支持并停止。
2. **询问节点情况**：确认用户手里属于哪种节点：
   - **单条节点**：单个固定 IP 的 SOCKS5 / HTTP(S)，或单个加密节点 VLESS / Trojan / SS（自建 VPS，出口即该节点）。
   - **两条节点（两跳）**：加密前置（VLESS / Trojan / SS）+ 固定 IP 出口（SOCKS5 / HTTP(S)），或两条 SOCKS5 / HTTP(S)（第一行前置、第二行出口）。
   - **还没有节点**：明确说明自备固定 IP 节点是使用前提，停止安装，不推荐任何节点商家。
   - **不支持的形态**：UDP 协议（hysteria2 / tuic / wireguard）、不认识的协议、三跳及以上、或作者自建多路链式（`EXIT_MODE=chain`）→ 说明一键安装不支持，指向 [docs/manual-install.md](docs/manual-install.md) 后停止。
3. **获取节点链接的方式**（优先剪贴板，不让链接进入对话）：
   - **推荐（剪贴板）**：把下面这段原话直接发给用户，等用户回复「好了」后再运行安装命令并追加 `--link-clipboard`（Windows 为 `-LinkClipboard`）：

```text
请复制你的节点链接（两跳就把两条一起复制，一行一条），不用发给我。安装程序会在你的电脑上直接读取剪贴板，不会显示链接，也不会传给我；读完会清空剪贴板。复制好了回复『好了』。
```

   - **用户提供文件路径**：只接收路径，在首页命令末尾追加 `--link-file <路径>`（Windows 为 `-LinkFile <路径>`），装完提醒用户自行删除该文件。
   - **用户直接把链接发进对话**（不推荐，链接会经过 AI 服务商）：写入仅当前用户可读的临时文件（POSIX `600` / Windows 仅当前用户 ACL），绝不放进命令行参数，用 `--link-file <临时文件>`（Windows 为 `-LinkFile`）运行，装完立即删除该临时文件。
   - **用户想自己手动粘贴**：直接给出 [README.md](README.md#快速开始) 对应平台的原样首页命令，让用户在自己的终端窗口运行并无回显粘贴。
4. **运行安装命令**：从 [README.md](README.md#快速开始) 复制当前锁定版本的对应平台命令并追加步骤 3 的参数，**绝不改写下载与 SHA-256 校验段**。
5. **节点前置探测与失败处理**：第 1–2 步会在下载 Tart / VirtualBox / 镜像 / Claude Code 之前先用宿主临时 mihomo 连续 3 次探测出口（检查连通性与固定 IP）。若失败，把安装器的大白话失败提示转述给用户并停下；按提示处理后原样重跑同一条命令（断点续跑）。需要排障时用引导准备的私有 Python 运行 `scripts/doctor.py` 导出脱敏诊断包。
6. **装完收尾与交付**：自动验收退出 0 且状态为 `DONE` 后，运行 `cchouse status --tag cc01` 确认状态（找不到 `cchouse` 时使用完整路径：Mac `~/.local/bin/cchouse`，Windows PowerShell `& "$env:LOCALAPPDATA\cchouse\bin\cchouse.cmd"`）。提醒用户在客机桌面打开 `check.html` 核对全绿后，自行在客机内登录 Claude（OAuth 授权链接只能在客机浏览器打开）。
7. **严禁操作**：
   - 严禁自行运行 `pbpaste`、`Get-Clipboard` 或任何读取剪贴板、`instance.env` 或链接文件内容的命令；
   - 严禁扫描用户电脑上的其他代理软件或配置；
   - 严禁修改宿主防火墙规则、系统代理、语言或时区设置；
   - 严禁代用户登录、注册、订阅或付款。

## 用公开版本安装

1. 根据宿主选择 [README 快速开始](README.md#快速开始) 的对应命令。Apple Silicon Mac 使用 Tart + Debian 13 arm64，Windows x64 使用 VirtualBox + Debian 13 amd64。Intel Mac 暂不支持。只读本次路线的文档。
2. **无 TTY 安装（剪贴板优先 / 链接文件）**：支持 1 条（SOCKS5 / HTTP(S) / VLESS / Trojan / SS）或 2 条（加密前置 + SOCKS5/HTTP 出口，或两条 SOCKS5/HTTP）链接。已有 `instance.env` 且未传 `--reconfigure` 时提示并忽略 `--link-clipboard`/`--link-file`。无上述参数且无交互 TTY 首次安装时，引导脚本与 `cchouse up` 会立即以退出码 `20` 停下并打印原样首页命令（Windows 经外层 PowerShell 调用时退出码可能显示为 `1`，以「需要你本人输入节点链接」提示为准）。已有保存节点配置时的续跑或重建无需交互，可在无 TTY 下直接完成。
3. 失败时读取该实例的 `state.json`，核对 `status`、`failed_step`、`next_action` 及退出码。使用安装器输出的工作目录和私有 Python 运行 `scripts/doctor.py`。先检查脱敏结果，按失败步骤排查，再重跑同一个锁定命令。默认私有根为 macOS 的 `~/Library/Application Support/cchouse` 或 Windows 的 `%LOCALAPPDATA%\cchouse`，默认标签为 `cc01`。
4. 自动验收须退出 0 且状态为 `DONE`。随后把桌面 `check.html` 核对与账号登录交给用户。Stripe 连通性 WARN 可接受。

## 安装器命令

| 操作 | 入口 |
|---|---|
| 安装或续跑 | 首页对应平台的锁定命令（可追加 `--link-clipboard` / `-LinkClipboard` 或 `--link-file <路径>` / `-LinkFile <路径>`）。维护入口为 `scripts/cchouse.py up --tag <TAG>` |
| 预览并卸载 | `scripts/cchouse.py remove --tag <TAG>`。自定义过 `--private-dir` 时也传原路径。核对归属清单后再确认 |
| 导出诊断 | `scripts/doctor.py`。有私有配置时加 `--env <instance.env>`，有状态记录时可加 `--state <state.json>`，客机正在运行时可加 `--vm-ssh` |
| 查状态 | `cchouse status --tag <TAG>`。不在 PATH 时用 Mac `~/.local/bin/cchouse`、Windows PowerShell `& "$env:LOCALAPPDATA\cchouse\bin\cchouse.cmd"` |
| 查版本 | `scripts/cchouse.py --version`。无需节点或实例 |

用引导准备的私有 Python 加 `-I` 执行这些脚本。具体路径及故障处理见 [运维手册](docs/operations.md)。

## 必须交给人的检查点（`HUMAN_WAIT`）

- **出口与凭据**。出口由用户选择。公开引导的链接由用户无回显输入，进阶安装的凭据由用户填写在私有 `instance.env` 中，macOS 文件权限为 `600`。渲染前运行 `preflight.py --env`。
- **宿主前提**。虚拟化未开启或安装卷空间不足时等用户处理。VirtualBox 由 `up` 安装。Windows 启用 VBS 时说明性能影响，不建议关闭 VBS。
- **桌面验收与登录**。自动验收退出 0 后，用户在客机桌面核对 `check.html` 并自行登录。Claude Code 的 OAuth 授权链接只能在客机浏览器里打开，严禁复制到宿主浏览器。

## 硬红线

- 禁止为恢复联网放宽 `pf` 或 `nftables`、添加 `DIRECT` 或改走宿主代理。断网时排查代理链路。
- 禁止编造凭据、扫描宿主其他代理软件获取配置、打开或输出 `instance.env` 原文。日志、公开 Issue、PR、Release 与提交不得泄露真实凭据、用户身份和节点信息。离线注入后立即删除明文渲染目录和临时 seed ISO。
- 禁止代用户登录、注册、订阅或付款。
- 禁止在待分发的 `.ova` 镜像预装 Claude Code。由宿主下载官方 npm 包、核对 integrity 后离线注入。
- Linux `nftables.conf` 禁止使用 `flush ruleset`。只操作独立的 `table inet cchouse_guard`，保留 mihomo 自建的 TUN 表。
- 只操作本次任务和安装记录能确认归属的 VM、快照、私有目录及缓存。隔离测试先验证虚拟机库存和缓存确实独立。

## 开发与发布

- 源码维护、手动渲染和导入见 [进阶安装](docs/manual-install.md)，验收详见 [verify.md](docs/verify.md)。修改模板或脚本后跑 `python3 -I -m unittest discover -s tests`，在相关真实宿主运行 `verify-linux.sh --fail-closed`，并更新 [CHANGELOG](CHANGELOG.md)。
- PR 需通过 Linux、macOS、Windows 的单元测试及 Bash/PowerShell 检查，共四项 CI。文档小改通过 CI 即可审合。安装流程、脚本或镜像变更须附对应真机证据，包含退出码、状态、验收结果和清理核对。
- 发布按 [引导制品手册](docs/bootstrap-release.md) 从冻结提交读取 `VERSION`，固定源码、引导脚本与镜像的 SHA-256。同一公开 tag 的字节永远不替换。修复发布问题要用新版本。草稿资产在核对前保持草稿。
- 发版验收要求 Mac 和 Windows 各连续两轮公开文档盲装，每轮从全新会话及干净测试环境开始，自动验收 0 FAIL，并真实打开客机桌面。涉及增强组件时，附实际窗口缩放与双向剪贴板证据。

## v1 macOS 维护

v1 Tart + macOS 已冻结，见 [维护手册](docs/build.md)。仅支持 `EXIT_MODE=chain`，`STRIPE_EXIT_MODE` 为 `front` 或 `tmh-chain`。改 `pf` 或 mihomo 前先后台运行 `guard.sh`，退出前保留 `/tmp/<TAG>`。客机不调用 `python3`。前置 xray 不可随意升至 `26.10.21+`。关机用 `host/stop-vm.sh`，脚本兼容 macOS bash 3.2。改模板后用 `scripts/import-existing.py` 核对，并跑 `verify.sh --fail-closed`。已登录快照记入 `RECOVERY_SNAPSHOT`，禁止克隆多台。剪贴板探针先校准 `LC_ALL=en_US.UTF-8`。远程交接先撤权再整理。
