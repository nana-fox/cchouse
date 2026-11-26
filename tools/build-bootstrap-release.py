#!/usr/bin/env python3
"""Build cchouse's version-pinned Release assets from one frozen commit."""

import argparse
import hashlib
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ("bootstrap-macos.sh", "bootstrap-windows.ps1")


def run_git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def readme_commands(version, mac_hash, windows_hash):
    release = f"https://github.com/nana-fox/cchouse/releases/download/{version}"
    return (
        'f=$(mktemp -t cchouse-bootstrap) && '
        f"curl -fsSLo \"$f\" {release}/bootstrap-macos.sh && "
        f"printf '%s  %s\\n' '{mac_hash}' \"$f\" | shasum -a 256 -c - && "
        '/bin/bash "$f" --cleanup-bootstrap\n',
        "$f = \"$env:TEMP\\cchouse-bootstrap-$([guid]::NewGuid().ToString('N')).ps1\"; "
        f"iwr {release}/bootstrap-windows.ps1 -UseBasicParsing -OutFile $f -ErrorAction Stop; "
        f"if ((Get-FileHash $f -Algorithm SHA256).Hash -ne '{windows_hash}') {{ throw 'SHA-256 mismatch' }}; "
        "Unblock-File -LiteralPath $f; powershell -NoProfile -ExecutionPolicy RemoteSigned -File $f -CleanupBootstrap\n",
    )


def build(version, commit, output):
    commit = run_git("rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}").decode().strip()
    source_version = run_git("show", f"{commit}:VERSION").decode("ascii").strip()
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", source_version):
        raise ValueError("VERSION must contain a full Release tag such as v2.0.0-preview.4")
    if version is not None and version != source_version:
        raise ValueError(f"requested version {version} does not match frozen VERSION {source_version}")
    version = source_version
    # The Windows entry point accepts only VirtualBox. A pre-#39 ZIP is unusable.
    run_git("cat-file", "-e", f"{commit}:guest-linux/virtualbox-manifest.json")
    templates = [run_git("show", f"{commit}:scripts/{name}").decode("utf-8-sig").replace("\r\n", "\n")
                 for name in TEMPLATES]
    if any(text.count("@CCHOUSE_VERSION@") != 1 or text.count("@CCHOUSE_SHA256@") != 1 for text in templates):
        raise ValueError("bootstrap template placeholders are missing or duplicated")

    output.mkdir(parents=True, exist_ok=False)
    archive_name = f"cchouse-{version}.zip"
    archive = run_git("archive", "--format=zip", "-0", "--prefix=cchouse/", commit)
    (output / archive_name).write_bytes(archive)
    archive_hash = sha256(archive)
    hashes = {}
    for name, template in zip(TEMPLATES, templates):
        source = template.replace("@CCHOUSE_VERSION@", version).replace("@CCHOUSE_SHA256@", archive_hash)
        asset = source.encode("utf-8-sig" if name.endswith(".ps1") else "utf-8")
        (output / name).write_bytes(asset)
        hashes[name] = sha256(asset)

    sums = {archive_name: archive_hash, **hashes}
    (output / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name, digest in sums.items()), encoding="ascii")
    mac, win = readme_commands(version, *(hashes[name] for name in TEMPLATES))
    (output / "README-commands.md").write_text(
        f"Source commit: `{commit}`\n\n```bash\n{mac}```\n\n```powershell\n{win}```\n", encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="optional assertion against VERSION in the frozen commit")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.version, args.commit, args.output)


if __name__ == "__main__":
    main()
