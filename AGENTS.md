# cchouse · 给 AI Agent 的执行手册

先读本文件，再按路线只读对应文档。**不要同时混读两个系统的安装手册。**

## 6 步状态机（按序执行，支持 `state.json` 断点续跑）

> 默认 `<private>` 根目录：macOS 为 `~/Library/Application Support/cchouse`（权限 `700`），Windows 为 `%LOCALAPPDATA%\cchouse`。确定 `<TAG>` 前可将状态写入 `<private>/state.json`，后续步骤用 `python3 -I scripts/preflight.py --state <private>/state.json --set-step <step> --set-status <status>` 推进，勿手改 JSON。

0. **环境自检**：运行 `python3 -I scripts/preflight.py --state <private>/state.json`（Windows 无 Python 先跑 `powershell -ExecutionPolicy Bypass -File scripts/preflight.ps1`）。按输出的 JSON `next_action` 推进，不要猜环境。
1. **前提与路线确认**（未满足则用 `--set-status HUMAN_WAIT` 停下等人）：
   - 确认用户已自备外部代理，选择 `EXIT_MODE=single`（仅 v2 Linux）或 `chain`（v1 macOS 仅支持 `chain` 且 `STRIPE_EXIT_MODE` 为 `front`/`tmh-chain`）。
   - Windows / Intel Mac 自动锁定 **VMware + Debian 13**（`vmware-linux`）；Apple Silicon Mac 优先推荐 **Tart + Debian 13 arm64**（`tart-linux`，轻量、支持 `single`/`chain`），需 macOS 原生桌面/Safari 高拟真时走 **Tart + macOS**（`macos-tart`，v1）。
   - 若本机缺 VMware / 未开 BIOS 虚拟化 / 磁盘严重不足，写入 `HUMAN_WAIT` 并停下来等用户处理完继续。
2. **按路线读文档**：见下方路由表，只加载当前路线所需文档。
3. **凭据与渲染**：引导用户在私有目录填好 `instance.env`（`chmod 600`），运行 `python3 -I scripts/preflight.py --env <instance.env>` 校验后，执行 `python3 -I scripts/render.py <instance.env> <private>/rendered`。
4. **部署与首次启动**：严格按对应路线 Runbook 执行；注入完成后立即删除 `<private>/rendered` 和临时 ISO。
5. **自动验收与人工交接**：运行 `verify.sh --fail-closed`（macOS）或 `verify-linux.sh --fail-closed`（Linux）。**只有退出码为 0 才算部署成功**，随后将桌面 `check.html` 核对与账号登录交给用户。

## 路由表

| 任务 | 只读这些文件 |
|---|---|
| 配置单节点 / 链式代理与 Stripe 分流 | [docs/proxy-modes.md](docs/proxy-modes.md) |
| Windows / Intel Mac（VMware + Debian 13） | [docs/install-vmware-linux.md](docs/install-vmware-linux.md) |
| Apple Silicon Mac（Tart + Debian 13 arm64，轻量首选） | [docs/install-tart-linux.md](docs/install-tart-linux.md) |
| Apple Silicon Mac（Tart + macOS，v1 高拟真） | [docs/build.md](docs/build.md) |
| 自建前置、加 UUID（链式模式） | [docs/front.md](docs/front.md) |
| 验收与手工核对项 | [docs/verify.md](docs/verify.md) |
| 换出口、快照重建、排障与远程交接 | [docs/operations.md](docs/operations.md) |

## 硬红线（绝对禁止）

- **绝不为了“恢复联网”去放宽防火墙（`pf`/`nftables`）、添加 `DIRECT` 规则、或改走宿主代理**。断网是保护生效，必须排查代理本身。
- **绝不编造凭据、不扫描宿主其他代理软件偷配置、不 `cat` 或在终端/日志/提交中打印 `instance.env` 真实值**；明文渲染产物与 seed ISO 用完即删。
- **绝不替用户登录、注册、订阅或付款**；必须提醒用户：**Claude Code 的 OAuth 登录链接只能在客机浏览器里打开，严禁复制到宿主浏览器**。
- **绝不把 Claude Code 预装进待分发的 `.ova` 镜像**（由宿主拉官方 npm tarball 核对 integrity 后拷入客机离线安装）。
- **Linux `nftables.conf` 绝不写 `flush ruleset`**（否则会清掉 `mihomo` 自建的 TUN 表），只操作独立的 `table inet cchouse_guard`。

## 必须停下来交给人的检查点（`HUMAN_WAIT`）

1. **填凭据与选出口**：出口节点参数由人写入私有 `instance.env`。
2. **安装 VMware / 开启 BIOS 虚拟化**：需人工下载安装与授权（Win11 开着 VBS 时告知性能影响，**不要建议用户关闭 VBS**）。
3. **最终验收与登录**：自动验收退出码 `0` 后，由人在客机桌面核对 `check.html` 全绿并自行登录。

## 维护与开发补充规则

- **v1（macOS Tart）安全约束**：改 `pf`/`mihomo` 前先后台跑 `guard.sh`（退出前勿删 `/tmp/<TAG>`）；客机不调用 `python3`；前置 `xray` 不可随手升至 `26.10.21+`；关机用 `host/stop-vm.sh`；脚本兼容 macOS bash 3.2；改模板后跑 `scripts/import-existing.py` 核对；已登录快照记入 `RECOVERY_SNAPSHOT` 且不可克隆多台；剪贴板探针先校准 `LC_ALL=en_US.UTF-8`；远程交接先撤权再整理。
- **改模板或脚本后**：跑 `python3 -I -m unittest discover -s tests` 并在实际环境跑通 `verify --fail-closed`，同步记录 [CHANGELOG.md](CHANGELOG.md)。
