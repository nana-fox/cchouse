# 发布一键安装资产

从包含 VirtualBox 路线及两个引导脚本的冻结提交打包。构建机需要 Git 和 Python，用户端用系统解包工具。

```bash
python3 -I tools/build-bootstrap-release.py --commit <frozen-commit> --output <new-directory>
```

1. 打包工具从冻结提交读取 `VERSION`。如传入 `--version`，其值必须相同。用 `SHA256SUMS` 核对生成的三个资产，并保留这些字节用于发布。
2. 把 `README-commands.md` 的两条命令同步到中英 README。文档更新使用后续提交，构建仍以第 1 步的冻结提交为准。
3. 将第 1 步的 `cchouse-<version>.zip`、`bootstrap-macos.sh`、`bootstrap-windows.ps1` 和 `SHA256SUMS` 上传至这个固定版本的 GitHub Release。从公开地址下载，复核三个资产的 SHA-256 与首页校验链一致。

首页使用已公开制品的 URL 和摘要。preview.4 沿用 preview.2 的两份镜像，按现有镜像清单核对 URL 与 SHA-256。待 preview.4 的公开命令可用时同步更新中英首页。发现已公开的资产有误时，创建新版本。

首次运行会把经过校验的 Python、源码和缺失的 Tart 解到 cchouse 私有目录。源码的 `--version` 需与引导版本一致。Mac 默认标签为 `cc01`，可在命令最后加 `--tag cc02`，或通过 `CCHOUSE_TAG` 指定标签。Windows 固定由 `up` 处理 VirtualBox。缺 OpenSSH Client 时按系统设置指引添加。重跑同一条命令可以续跑，已经校验的下载会复用。

Windows 首页命令在校验成功后解除临时脚本的互联网区域标记，用该子进程的 `RemoteSigned` 策略运行。系统和组策略照常生效。
