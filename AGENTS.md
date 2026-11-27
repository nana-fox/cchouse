# cchouse · AI Agent 执行手册

## 用公开版本安装

1. 根据宿主选择 [README 快速开始](README.md#快速开始) 的对应命令。Apple Silicon Mac 使用 Tart + Debian 13 arm64，Windows x64 使用 VirtualBox + Debian 13 amd64。Intel Mac 暂不支持。只读本次路线的文档。
2. 首次安装需要输入节点链接，且当前终端无交互 TTY 时（如 AI 工具调用），引导脚本与 `cchouse up` 会立即以退出码 `20` 停下并打印原样首页命令；请把该命令交给用户在**自己的终端窗口**运行，由用户本人在无回显提示处粘贴自备的 SOCKS5 或 HTTP(S) 节点（AI 不索要、不读取、不保存链接）。已有保存节点配置时的续跑或重建无需交互，可在无 TTY 下直接完成。VLESS、Trojan、Shadowsocks 及两跳配置按 [进阶文档](docs/proxy-modes.md) 操作。
3. 失败时读取该实例的 `state.json`，核对 `status`、`failed_step`、`next_action` 及退出码。使用安装器输出的工作目录和私有 Python 运行 `scripts/doctor.py`。先检查脱敏结果，按失败步骤排查，再重跑同一个锁定命令。默认私有根为 macOS 的 `~/Library/Application Support/cchouse` 或 Windows 的 `%LOCALAPPDATA%\cchouse`，默认标签为 `cc01`。
4. 自动验收须退出 0 且状态为 `DONE`。随后把桌面 `check.html` 核对与账号登录交给用户。Stripe 连通性 WARN 可接受。

## 安装器命令

| 操作 | 入口 |
|---|---|
| 安装或续跑 | 首页对应平台的锁定命令。维护入口为 `scripts/cchouse.py up --tag <TAG>` |
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
