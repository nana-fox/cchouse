# cchouse

一台**固定出口、断网即停、环境隔离**的虚拟机环境，用来运行 Claude（网页 + Claude Code CLI）。
这个仓库是环境的**唯一配置源**：纯文本模板、渲染脚本、自动验收和文档都在这里，**不包含凭据和大镜像**。

---

## 一句话交给你的 AI Agent 安装

把下面这句话发给你的 AI Agent（Codex、Claude Code、Cursor、Antigravity 等）：

> 帮我拉取 `https://github.com/nana-fox/cchouse`（当前使用 `main` 分支或指定 commit；首个正式 release tag 将在 v2 黄金镜像发布后提供），严格按根目录 `AGENTS.md` 的 6 步状态机运行 `preflight` 检查我的电脑环境和代理前提，引导我选择路线、安装并完成 `--fail-closed` 验收。

---

## 前提条件（所有路线通用）

1. **必须自备外部 IP 代理**（`cchouse` 不提供代理）：
   - **单节点模式（`EXIT_MODE=single`）**：1 个固定出口节点（SOCKS5 / HTTP / VLESS / Trojan / Shadowsocks / SSH；不支持 Hysteria2 / TUIC 等纯 UDP 协议）。**注：当前仅支持 v2 Linux（VMware）路线。**
   - **链式模式（`EXIT_MODE=chain`）**：前置节点（自建 VPS / CF 前置 / 机场）→ 固定 ISP 出口（**v1 macOS Tart 与 v2 Linux 均支持**）；若所用 ISP 屏蔽 Stripe（如 Webshare），可额外开启 `STRIPE_EXIT_MODE`（v1 macOS 必须为 `front` 或 `tmh-chain`）。
   - 详细配置见 [docs/proxy-modes.md](docs/proxy-modes.md)。
2. **本机硬件建议**：内存 ≥ 16 GB；磁盘剩余空间（Tart macOS 客机 ≥ 50 GB，VMware Debian 13 客机 ≥ 25 GB）。

---

## 系统与路线选择

| 你的电脑 | 状态 | 路线 | 客机环境 | 人工准备（一次性） | 自动部署耗时（参考） | 入口文档 |
|---|---|---|---|---|---|---|
| **Apple Silicon Mac** | **已实机验收（v1）** | **优先推荐：Tart** | macOS + Safari + Claude Code CLI | `brew install cirruslabs/cli/tart` | 首次拉取 macOS 镜像（~25–30 GB）约 30–60 分钟 | [docs/build.md](docs/build.md) |
| **Windows（x64，家庭版 / Pro）** | **开发预览（v2，待真机实测）** | **VMware Workstation Pro** | Debian 13 + Chromium + Claude Code CLI | 注册 Broadcom 下载安装免费版 VMware、开启 BIOS 虚拟化 | 发布黄金 `.ova` 后约 5–10 分钟 | [docs/install-vmware-linux.md](docs/install-vmware-linux.md) |
| **Intel Mac** | **开发预览（v2，待真机实测）** | **VMware Fusion Pro** | Debian 13 + Chromium + Claude Code CLI | 注册 Broadcom 下载安装免费版 VMware Fusion | 发布黄金 `.ova` 后约 5–10 分钟 | [docs/install-vmware-linux.md](docs/install-vmware-linux.md) |

> 注：v2.0 黄金镜像当前处于 `unreleased-dev`（尚未发布公开 `.ova` 下载链接），首版仅覆盖 `amd64`（Windows x64 与 Intel Mac）；Apple Silicon Mac 走已验收的 Tart 路线（注意 Tart 初装阶段与冷启动边界见 [docs/architecture.md#已知限制](docs/architecture.md#已知限制)）。

---

## 核心原则

1. **一号、一机、一出口**：一台环境只登一个账号，只走固定出口。
2. **出问题就断网（fail-closed）**：代理挂了、入口变了、规则写错，结果只能是断网，绝不退回宿主直连。
   - macOS 客机靠 `pf` 兜底；Linux 客机靠「出厂默认全断 `nftables`（排在 `network-pre.target` 前）+ 网卡首启断开 + `meta skuid mihomo` 白名单」三层兜底。
3. **环境和出口一致**：时区、语言、地区、键盘、随机主机名/machine-id 都要和出口 IP 所在地匹配；自动验收对比出口 IP 时区与客机时区。
4. **网页与 CLI 全在客机内闭环**：只用官方网页与官方 Claude Code CLI；**OAuth 授权链接必须在客机浏览器内打开，严禁复制到宿主浏览器**。

---

## 目录

```text
guest/                 v1 macOS 客机模板（Tart，保持原样）
guest-linux/           v2 Debian 13 客机模板（VMware / 通用 Linux）
host/                  v1 Apple Silicon 宿主机启停与巡检脚本
scripts/               preflight.py(.ps1) 环境自检 / render.py 渲染 / verify.sh & verify-linux.sh 验收
docs/                  架构、代理模式、分路线安装手册、验收与运维
tests/                 render / preflight / 防泄漏断言单元测试
instance.env.example        v1 macOS 链式实例模板
instance.env.linux.example  v2 Linux 实例模板（支持 single 与 chain）
```

---

## 快速上手（命令行）

```bash
# 0. 安装前先启用防凭据提交钩子
git config core.hooksPath .githooks

# 1. 运行环境自检（Windows 无 Python 时先跑 powershell -File scripts/preflight.ps1）
python3 -I scripts/preflight.py

# 2. 准备实例参数（放私有目录，不进仓库）
#    macOS Tart 路线复制 instance.env.example；VMware Linux 路线复制 instance.env.linux.example
D=~/Library/Application\ Support/cchouse/<TAG>; mkdir -p "$D"; chmod 700 "$D"
cp instance.env.linux.example "$D/instance.env"; chmod 600 "$D/instance.env"

# 3. 渲染客机文件（输出含明文凭据，注入后立即删除）
python3 -I scripts/render.py "$D/instance.env" "$D/rendered"

# 4. 按对应路线文档部署并验收
#    macOS Tart:    scripts/verify.sh "$D/instance.env" --fail-closed
#    VMware Linux:  scripts/verify-linux.sh "$D/instance.env" --fail-closed
```

---

## 凭据与变更

- 所有凭据只在私有目录的 `instance.env`（`chmod 600`），真实值来源于密码管理器。仓库内只允许 `<占位符>`，`.githooks/pre-commit` 会拦截疑似凭据。
- 详细架构见 [docs/architecture.md](docs/architecture.md)，变更记录见 [CHANGELOG.md](CHANGELOG.md)。
