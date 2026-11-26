# 发布一键安装资产

从包含 VirtualBox 路线及两个引导脚本的冻结提交打包。构建机需要 Git 和 Python，用户端用系统解包工具。

```bash
python3 -I tools/build-bootstrap-release.py --version v2.0.0-preview.3 --commit <frozen-commit> --output <new-directory>
```

1. 将 `cchouse-<version>.zip`、`bootstrap-macos.sh`、`bootstrap-windows.ps1` 和 `SHA256SUMS` 上传至这个固定版本的 GitHub Release。
2. 从公开地址下载，确认三个资产的 SHA-256 与 `SHA256SUMS` 一致。
3. 把 `README-commands.md` 的两条命令同步到中英 README。用后续文档提交更新首页，保持已经发布的源码包和引导脚本不变。

首次运行会把经过校验的 Python、源码和缺失的 Tart 解到 cchouse 私有目录。Windows 固定由 `up` 处理 VirtualBox。缺 OpenSSH Client 时按系统设置指引添加。重跑同一条命令可以续跑，已经校验的下载会复用。

Windows 首页命令在校验成功后解除临时脚本的互联网区域标记，用该子进程的 `RemoteSigned` 策略运行。系统和组策略照常生效。
