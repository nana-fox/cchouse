#!/usr/bin/env python3
"""将 render.py 渲染目录（及可选的 claude-code.tgz）打包成卷标为 CCHOUSE 的只读配置 ISO。

用法：
  python3 -I scripts/make-seed-iso.py <rendered目录> <输出.iso路径> [--claude-tarball <claude-code.tgz>]

- 输出 ISO 含明文凭据，权限 600，首次开机注入完成后务必立即卸载并删除。
- 跨平台自动选用本机工具：
  - macOS：系统自带 hdiutil
  - Linux：xorriso / mkisofs / genisoimage
  - Windows：优先 xorriso / mkisofs / oscdimg，缺省回退至系统自带 PowerShell IMAPI2FS COM（零第三方依赖）
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def pick_iso_tool():
    if platform.system() == "Darwin" and shutil.which("hdiutil"):
        return ("hdiutil", shutil.which("hdiutil"))
    for name in ("xorriso", "mkisofs", "genisoimage", "oscdimg"):
        p = shutil.which(name) or shutil.which(name + ".exe")
        if p:
            return (name, p)
    if platform.system() == "Windows":
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            return ("imapi2", ps)
    return (None, None)


def build_iso_imapi2(ps_exe, stage_dir, out_iso):
    # PowerShell 无法直接调用裸 System.__ComObject 的 IStream.Read，通过内置 C# ComTypes.IStream 桥接写入
    ps_script = r"""
param([string]$Src, [string]$Dst)
$ErrorActionPreference = "Stop"
Add-Type -TypeDefinition @"
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
public static class CchouseIsoWriter {
    public static void Write(object comStream, string dstPath) {
        IStream stream = (IStream)comStream;
        byte[] buf = new byte[65536];
        IntPtr readPtr = Marshal.AllocCoTaskMem(sizeof(int));
        try {
            using (FileStream fs = File.Create(dstPath)) {
                while (true) {
                    stream.Read(buf, buf.Length, readPtr);
                    int read = Marshal.ReadInt32(readPtr);
                    if (read <= 0) break;
                    fs.Write(buf, 0, read);
                }
            }
        } finally {
            Marshal.FreeCoTaskMem(readPtr);
        }
    }
}
"@
$img = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
$img.VolumeName = "CCHOUSE"
$img.FileSystemsToCreate = 3
$img.Root.AddTree($Src, $false)
$res = $img.CreateResultImage()
[CchouseIsoWriter]::Write($res.ImageStream, $Dst)
"""
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as tf:
        tf.write(ps_script)
        script_path = tf.name
    try:
        cp = subprocess.run(
            [ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path, str(stage_dir), str(out_iso)],
            capture_output=True,
            text=True,
        )
        if cp.returncode != 0:
            sys.exit(f"Windows IMAPI2FS 生成 ISO 失败：{cp.stderr.strip() or cp.stdout.strip()}")
    finally:
        Path(script_path).unlink(missing_ok=True)


def build_iso(stage_dir, out_iso):
    kind, tool = pick_iso_tool()
    if not tool:
        sys.exit(
            "未找到可用的 ISO 制作工具（Linux 需 xorriso/mkisofs/genisoimage；macOS 自带 hdiutil；Windows 自带 PowerShell IMAPI2FS）。"
        )
    if out_iso.exists():
        out_iso.unlink()

    if kind == "imapi2":
        build_iso_imapi2(tool, stage_dir, out_iso)
    else:
        if kind == "hdiutil":
            cmd = [tool, "makehybrid", "-iso", "-joliet", "-default-volume-name", "CCHOUSE", "-o", str(out_iso), str(stage_dir)]
        elif kind == "xorriso":
            cmd = [tool, "-as", "mkisofs", "-V", "CCHOUSE", "-J", "-R", "-o", str(out_iso), str(stage_dir)]
        elif kind in ("mkisofs", "genisoimage"):
            cmd = [tool, "-V", "CCHOUSE", "-J", "-R", "-o", str(out_iso), str(stage_dir)]
        elif kind == "oscdimg":
            cmd = [tool, "-lCCHOUSE", "-j1", str(stage_dir), str(out_iso)]
        else:
            sys.exit(f"未知 ISO 工具：{kind}")

        cp = subprocess.run(cmd, capture_output=True, text=True)
        if cp.returncode != 0:
            sys.exit(f"{kind} 生成 ISO 失败：{cp.stderr.strip() or cp.stdout.strip()}")
        if not out_iso.exists() and Path(str(out_iso) + ".iso").exists():
            Path(str(out_iso) + ".iso").rename(out_iso)

    os.chmod(out_iso, 0o600)


def main():
    parser = argparse.ArgumentParser(description="Build LABEL=CCHOUSE provisioning ISO")
    parser.add_argument("rendered_dir")
    parser.add_argument("output_iso")
    parser.add_argument("--claude-tarball", default=None)
    args = parser.parse_args()

    rendered = Path(args.rendered_dir)
    if not rendered.is_dir():
        sys.exit(f"渲染目录不存在：{rendered}")
    if not (rendered / "config.yaml").is_file() or not (rendered / "nftables.conf").is_file():
        sys.exit("渲染目录缺少 config.yaml 或 nftables.conf，请先运行 render.py")

    out_iso = Path(args.output_iso)
    out_iso.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "CCHOUSE"
        shutil.copytree(rendered, stage)
        if args.claude_tarball:
            tb = Path(args.claude_tarball)
            if not tb.is_file():
                sys.exit(f"Claude Code tarball 不存在：{tb}")
            shutil.copy2(tb, stage / "claude-code.tgz")
        build_iso(stage, out_iso)

    print(out_iso)


if __name__ == "__main__":
    main()
