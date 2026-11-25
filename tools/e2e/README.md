# cchouse E2E 一键真机验证工具 (`tools/e2e/`)

一条命令完成 Mac（Tart / arm64）与 GTR（Windows VMware / amd64）的真机端到端验证，输出实时刷新的 HTML 报告、JSON 状态文件与客机桌面 PNG 截图，测完自动清理测试机与临时明文。

## 快速用法

```bash
# 快速模式（克隆黄金镜像 + 覆盖当前工作区 guest-linux 脚本）
tools/e2e/run.sh --target mac --mode fast
tools/e2e/run.sh --target gtr --mode fast
tools/e2e/run.sh --target both --mode fast

# 发布模式（从指定 .tvm / .ova 导入）
tools/e2e/run.sh --target mac --mode release --tvm /path/to/cchouse-debian13-arm64.tvm
tools/e2e/run.sh --target gtr --mode release --ova 'C:\path\to\cchouse-debian13-amd64.ova'
```

## 私有配置（不进仓库）

默认读取 `~/Library/Application Support/cchouse/e2e/e2e.conf`（也可用 `--config <path>` 或环境变量 `CCHOUSE_E2E_CONFIG` 指定），目录权限 `700`，文件权限 `600`：

```bash
MAC_GOLDEN_VM="cchouse-debian13-arm64"
MAC_ENV_FILE="$HOME/Library/Application Support/cchouse/e2e/mac.env"
GTR_ENV_FILE="$HOME/Library/Application Support/cchouse/e2e/gtr.env"
GTR_SSH_HOST="192.168.124.21"
GTR_SSH_USER="ddnio"
GTR_SSH_KEY="$HOME/.ssh/cchouse_gtr_ed25519"
GTR_GOLDEN_OVA="C:\\Users\\ddnio\\cchouse-ova-build\\cchouse-debian13-amd64.ova"
GTR_GOLDEN_VMX="C:\\Users\\ddnio\\cchouse-e2e-base\\cchouse-debian13-amd64\\cchouse-debian13-amd64.vmx"
GTR_WORK_DIR="C:\\Users\\ddnio\\AppData\\Local\\cchouse\\e2e"
```

## 验证覆盖项

1. **当前工作区脚本覆盖**：在 Mac 侧使用当前仓库的 `scripts/render.py` 与 `guest-linux/` 渲染并打包 `seed.iso`（含在 Mac 侧下载并校验 SHA-512 的 Claude Code 对应架构原生包，避免 Windows 宿主机代理 TLS 问题）。
2. **完整三层注入流**：离线挂载 `seed.iso` 首启注入 → 卸载并删除 `seed.iso` → 联网重启 → SSH 连通。
3. **红线与出口验收**：执行 `scripts/verify-linux.sh --fail-closed`（出口 IP/国家/ASN/时区、IPv6/直连/路由器阻断、DNS 假 IP、nftables 规则摘要、最小 sudoers、停 mihomo 断网恢复）。
4. **扩展检查与桌面截图**：Stripe 分流可达性、`claude --version`、中文桌面进程（`lightdm`/`openbox`/`tint2`/`fcitx5`/`chromium`）、中文字体、镜像来源 `/etc/cchouse-image-release`，并在客机 `DISPLAY=:0` 用 `xwd` 截全屏回传转 PNG。

## 报告输出

每次运行生成目录 `~/Library/Application Support/cchouse/e2e-runs/<YYYYMMDD-HHMMSS>-<target>/`：

- `report.html`：运行中每 3 秒自动刷新；失败时第一行高亮标出失败步骤与原因；完成后内嵌客机桌面截图 `desktop.png`。
- `status.json`：结构化状态与各步骤起止时间、耗时、结果。
- `desktop.png`：客机 `DISPLAY=:0` 全屏桌面截图（含 Chromium `check.html`）。
