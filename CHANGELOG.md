# 变更记录

## v2.0-preview

- 新增跨平台 Linux 客机模板（`guest-linux/`，Debian 13 + VMware Workstation/Fusion Pro 路线，开发预览）。
- 出口层与客机隔离层解耦：支持 `EXIT_MODE=single`（单节点）与 `EXIT_MODE=chain`（前置 → 固定 ISP），可选 `STRIPE_EXIT_MODE`。
- 新增面向外部 AI Agent 的 6 步状态机手册（`AGENTS.md`）、`scripts/preflight.py(.ps1)`、`scripts/make-seed-iso.py`、`scripts/verify-linux.sh` 与单元测试 `tests/test_v2.py`。
- 保留 Apple Silicon Mac `tart` + macOS 客机路线（`guest/`、`host/`）。
