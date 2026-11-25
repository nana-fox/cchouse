# 变更记录

## v2.0-preview

- 新增跨平台 Linux 客机模板（`guest-linux/`，Debian 13 + VMware Workstation/Fusion Pro 路线，开发预览）。
- 出口层与客机隔离层解耦：支持 `EXIT_MODE=single`（单节点）与 `EXIT_MODE=chain`（前置 → 固定 ISP），可选 `STRIPE_EXIT_MODE`。
- 新增面向外部 AI Agent 的 6 步状态机手册（`AGENTS.md`）、`scripts/preflight.py(.ps1)`、`scripts/make-seed-iso.py`、`scripts/verify-linux.sh` 与单元测试 `tests/test_v2.py`。
- 保留 Apple Silicon Mac `tart` + macOS 客机路线（`guest/`、`host/`）。
- 完善 v1 macOS 文档与模板：`docs/build.md` 补充 `mihomo` / `tart-guest-agent` 下载与 sha256 核对命令、明确基础镜像版本及本地 vanilla 底包 CoW 复用说明、移除私有目录硬引用；`instance.env.example` 显式声明 `GUEST_OS`/`EXIT_MODE` 并将外置盘 `VM_STORAGE` 默认置空。
