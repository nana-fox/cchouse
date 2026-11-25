#!/usr/bin/env python3
"""Fast, reliable Claude Code platform tarball fetcher with npm SHA-512 SRI verification.

Uses curl (preferring direct connection to registry.npmjs.org, falling back to system proxy)
to avoid Python urllib hanging on SOCKS5/HTTP proxy or TLS EOF issues on Windows/macOS.
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PLATFORMS = {
    "linux-x64": "@anthropic-ai/claude-code-linux-x64",
    "linux-arm64": "@anthropic-ai/claude-code-linux-arm64",
}


def curl_download(url: str, dst: Path, timeout: int = 300) -> None:
    urls = [url]
    if url.startswith("https://registry.npmjs.org/") and url.endswith(".tgz"):
        # Try npmmirror first for fast China download; official npmjs SHA-512 SRI still verified below.
        urls.insert(0, url.replace("https://registry.npmjs.org/", "https://registry.npmmirror.com/", 1))
    last_err = ""
    for candidate in urls:
        attempts = [
            ["curl", "-fsSL", "--noproxy", "*", "--max-time", str(timeout), "-o", str(dst), candidate],
            ["curl", "-fsSL", "--max-time", str(timeout), "-o", str(dst), candidate],
        ]
        for cmd in attempts:
            cp = subprocess.run(cmd, capture_output=True, text=True)
            if cp.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
                return
            last_err = cp.stderr.strip() or f"exit {cp.returncode}"
    sys.exit(f"Failed to download {url}: {last_err}")


def verify_sri_file(path: Path, integrity: str) -> int:
    if not integrity.startswith("sha512-"):
        sys.exit(f"Unsupported integrity algorithm: {integrity}")
    expected = integrity.split("-", 1)[1]
    h = hashlib.sha512()
    total = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    actual = base64.b64encode(h.digest()).decode("ascii")
    if actual != expected:
        sys.exit(f"SHA-512 integrity mismatch (expected {expected[:16]}..., got {actual[:16]}...)")
    return total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    parser.add_argument("--version", default="latest")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    pkg = PLATFORMS[args.platform]
    encoded_pkg = pkg.replace("/", "%2f")
    meta_url = f"https://registry.npmjs.org/{encoded_pkg}/{args.version}"

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        meta_file = Path(td) / "meta.json"
        curl_download(meta_url, meta_file, timeout=30)
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        dist = meta.get("dist") or {}
        tarball_url = dist.get("tarball")
        integrity = dist.get("integrity")
        version = meta.get("version")
        if not tarball_url or not integrity:
            sys.exit(f"npm metadata missing tarball or integrity: {meta_url}")

        tmp_tgz = Path(td) / "claude-code.tgz"
        curl_download(tarball_url, tmp_tgz, timeout=300)
        size = verify_sri_file(tmp_tgz, integrity)
        os.replace(tmp_tgz, out_path)
        os.chmod(out_path, 0o600)

    print(json.dumps({
        "ok": True,
        "package": pkg,
        "version": version,
        "integrity": integrity,
        "bytes": size,
        "out": str(out_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
