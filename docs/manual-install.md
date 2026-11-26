# 手动安装与进阶说明

> 本页供进阶配置、维护与开发使用。公开版安装见 [README 快速开始](../README.md#快速开始)。

---

## 交给 AI Agent 安装

把下面这句话发给你的 AI Agent（Codex、Claude Code、Cursor、Antigravity 等）：

> 按 [cchouse 的 AGENTS.md](https://github.com/nana-fox/cchouse/blob/main/AGENTS.md) 用首页的公开锁定命令安装。节点由我在无回显提示中输入。请完成自动验收，随后把客机桌面检查与登录交给我。

---

## 前提条件（所有路线通用）

1. **必须自备外部 IP 代理**（`cchouse` 不提供代理）：
   - **单节点模式（`EXIT_MODE=single`，默认推荐）**：1 个固定出口节点（SOCKS5 / HTTP / VLESS / Trojan / Shadowsocks / SSH；不支持 Hysteria2 / TUIC 等纯 UDP 协议）。仅 v2 Linux 客机支持。若该节点拦截 Stripe（如 Webshare），只影响在 claude.ai 网页上付款，安装验收会给出提示但不拦截；需要时可加一个 Stripe 备用节点。
   - **链式模式（`EXIT_MODE=chain`，进阶）**：前置节点（自建 VPS / CF 前置 / 机场）→ 固定 ISP 出口（v1 macOS Tart 与 v2 Linux 均支持）；若所用 ISP 屏蔽 Stripe，可额外开启 `STRIPE_EXIT_MODE`（v1 macOS 必须为 `front` 或 `tmh-chain`）。
   - 详细配置见 [proxy-modes.md](proxy-modes.md)，自建前置见 [front.md](front.md)。
2. **本机硬件建议**：内存 16 GB。完整安装含下载，Windows 至少需要 7 GB、建议 10 GB，Apple Silicon 的 Debian 13 客机至少需要 9 GB、建议 12 GB。v1 Tart macOS 客机建议至少 50 GB。

---

## 路线与入口文档

| 你的电脑 | 路线 | 客机环境 | 入口文档 |
|---|---|---|---|
| Apple Silicon Mac，macOS 13.0 或更新版本 | Tart（`tart-linux`） | Debian 13 arm64 + Chromium + Claude Code CLI | [install-tart-linux.md](install-tart-linux.md) |
| Windows x64 | VirtualBox（`virtualbox-linux`） | Debian 13 amd64 + Chromium + Claude Code CLI | [install-virtualbox-linux.md](install-virtualbox-linux.md) |
| Intel Mac | 暂不支持 | — | — |

> v1 macOS 客机已冻结，不再新增功能，只保留文档供已有用户维护。仅支持 `EXIT_MODE=chain`。已知限制见 [architecture.md#已知限制](architecture.md#已知限制)。

---

## Mac 源码直装（维护与开发）

准备 [Homebrew](https://brew.sh)、Git 与 Python 3。用维护任务指定的分支或提交运行，日常用户可直接使用首页公开命令。

```bash
brew install openai/tools/tart
git clone https://github.com/nana-fox/cchouse.git
cd cchouse
python3 -I scripts/cchouse.py up --tag cc01
```

## 手动快速上手（命令行）

```bash
# 0. 安装前先启用防凭据提交钩子
git config core.hooksPath .githooks

# 1. 运行环境自检（Windows 无 Python 时先跑 powershell -File scripts/preflight.ps1）
python3 -I scripts/preflight.py

# 2. 准备实例参数（放私有目录，不进仓库）
#    v2 Linux 路线复制 instance.env.linux.example；v1 macOS Tart 路线复制 instance.env.example
D=~/Library/Application\ Support/cchouse/<TAG>; mkdir -p "$D"; chmod 700 "$D"
cp instance.env.linux.example "$D/instance.env"; chmod 600 "$D/instance.env"

# 3. 渲染前必须先填好 instance.env（渲染器缺 SSH_PUBKEY 会直接退出）：
#    a) 生成专用 SSH 密钥，把 .pub 单行内容填入 SSH_PUBKEY
ssh-keygen -t ed25519 -f "$D/id_ed25519" -N ""
#    b) 填节点协议/地址/认证参数（SINGLE_*）与预期出口 EXPECTED_EGRESS_IP/LOC/ASN、TIMEZONE
#    c) Windows 使用模板中的 NAT 网段与回环地址，SSH_PORT 须与 VirtualBox 转发端口一致
python3 -I scripts/preflight.py --env "$D/instance.env"

# 4. 渲染客机文件（输出含明文凭据，注入后立即删除）
python3 -I scripts/render.py "$D/instance.env" "$D/rendered"

# 5. 按对应路线文档部署并验收（退出码为 0 后，再到虚拟机桌面打开检查页复核）
#    v2 Linux:      scripts/verify-linux.sh "$D/instance.env" --fail-closed
#    v1 macOS Tart: scripts/verify.sh "$D/instance.env" --fail-closed
```

---

## 目录结构

```text
guest-linux/           v2 Debian 13 客机模板（VirtualBox / Tart）
guest/                 v1 macOS 客机模板（Tart，已冻结）
host/                  v1 Apple Silicon 宿主机启停与巡检脚本
scripts/               preflight.py(.ps1) 环境自检 / render.py 渲染 / verify.sh & verify-linux.sh 验收 / doctor.py 诊断包
docs/                  架构、代理模式、分路线安装手册、验收与运维
tests/                 render / preflight / 防泄漏断言单元测试
instance.env.example        v1 macOS 链式实例模板
instance.env.linux.example  v2 Linux 实例模板（支持 single 与 chain）
```

---

## 凭据与变更

- 所有凭据只在私有目录的 `instance.env`（`chmod 600`），真实值来源于密码管理器。仓库内只允许 `<占位符>`，`.githooks/pre-commit` 会拦截疑似凭据。
- 架构细节见 [architecture.md](architecture.md)，验收项见 [verify.md](verify.md)，换出口与排障见 [operations.md](operations.md)，变更记录见 [CHANGELOG.md](../CHANGELOG.md)。
