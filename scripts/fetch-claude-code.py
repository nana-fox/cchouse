#!/usr/bin/env python3
"""从 npm registry 宿主机下载官方 @anthropic-ai/claude-code 平台原生包并校验 sha512 integrity。

用法：
  python3 -I scripts/fetch-claude-code.py --platform linux-x64 --out /path/to/claude-code.tgz [--version latest]

说明：
- 平台原生包内只包含单个独立可执行二进制 package/claude，无需在客机安装 Node.js 或 npm。
- 宿主机仅访问 registry.npmjs.org，不直接访问 Anthropic 官网；客机离线解压即可使用。
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

PLATFORMS = {
    "linux-x64": "@anthropic-ai/claude-code-linux-x64",
    "linux-arm64": "@anthropic-ai/claude-code-linux-arm64",
    "darwin-arm64": "@anthropic-ai/claude-code-darwin-arm64",
    "darwin-x64": "@anthropic-ai/claude-code-darwin-x64",
}


def fetch_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "cchouse-fetch/1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def verify_sri(data, integrity):
    if not integrity.startswith("sha512-"):
        raise ValueError(f"不支持的 integrity 算法：{integrity}")
    expected = integrity.split("-", 1)[1]
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if actual != expected:
        raise ValueError(f"SHA-512 integrity 校验失败（期望 {expected[:16]}...，实际 {actual[:16]}...）")


def main():
    parser = argparse.ArgumentParser(description="Fetch & verify Claude Code native platform tarball")
    parser.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    parser.add_argument("--version", default="latest")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    pkg = PLATFORMS[args.platform]
    encoded_pkg = pkg.replace("/", "%2f")
    meta_url = f"https://registry.npmjs.org/{encoded_pkg}/{args.version}"
    meta = fetch_json(meta_url)
    dist = meta.get("dist") or {}
    tarball_url = dist.get("tarball")
    integrity = dist.get("integrity")
    version = meta.get("version")
    if not tarball_url or not integrity:
        sys.exit(f"npm 元数据缺少 tarball 或 integrity：{meta_url}")

    req = urllib.request.Request(tarball_url, headers={"User-Agent": "cchouse-fetch/1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    verify_sri(data, integrity)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    if os.name != "nt":
        os.chmod(out_path, 0o600)

    print(json.dumps({
        "ok": True,
        "package": pkg,
        "version": version,
        "integrity": integrity,
        "bytes": len(data),
        "out": str(out_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
