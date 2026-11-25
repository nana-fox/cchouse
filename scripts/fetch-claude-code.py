#!/usr/bin/env python3
"""从 npm registry 宿主机下载官方 @anthropic-ai/claude-code 平台原生包并校验 sha512 integrity。

用法：
  python3 -I scripts/fetch-claude-code.py --platform linux-x64 --out /path/to/claude-code.tgz [--version latest]

说明：
- 平台原生包内只包含单个独立可执行二进制 package/claude，无需在客机安装 Node.js 或 npm。
- 宿主机仅访问 registry.npmjs.org，不直接访问 Anthropic 官网；客机离线解压即可使用。
- 下载/续传/校验逻辑在 scripts/downloads.py，与 scripts/cchouse.py up 共用；已校验通过的同版本包直接跳过。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import downloads  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Fetch & verify Claude Code native platform tarball")
    parser.add_argument("--platform", choices=sorted(downloads.CLAUDE_PLATFORMS), required=True)
    parser.add_argument("--version", default="latest")
    parser.add_argument("--out", required=True)
    parser.add_argument("--npm-mirror", default=None, help=f"tarball 镜像源（也可用环境变量 {downloads.NPM_MIRROR_ENV}）；元数据与 integrity 仍取官方 registry")
    args = parser.parse_args()

    prog = downloads.Progress(f"Claude Code {args.platform}")
    try:
        res = downloads.run_with_progress(
            prog, lambda p: downloads.fetch_claude_code(args.platform, args.out, version=args.version, progress=p, mirror=args.npm_mirror)
        )
    except downloads.DownloadError as exc:
        sys.exit(str(exc))
    print(json.dumps(dict(res, ok=True), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
