#!/usr/bin/env python3
"""cchouse 桌面快捷启动图标与命令入口管理。

按平台分派：
  - macOS (_mac): osacompile 生成 ~/Applications/cchouse (<tag>).app（调用 cchouse open --tag <tag>，不弹终端）
                  + 桌面替身 ~/Desktop/cchouse (<tag>)。
  - Windows (_windows): 预留给 Windows 分支（1B）实现 .lnk 快捷方式。

所有创建的图标路径记录在 state["launchers"]（字符串列表），cchouse remove 预览并清理。
"""
import base64
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def cli_entry_path(home=None, platform_name=None):
    """返回平台命令入口路径：macOS ~/.local/bin/cchouse，Windows %LOCALAPPDATA%\\cchouse\\bin\\cchouse.cmd。"""
    sys_name = platform.system() if platform_name is None else platform_name
    if sys_name == "Windows":
        if home is not None:
            return Path(home) / "AppData" / "Local" / "cchouse" / "bin" / "cchouse.cmd"
        local_app = os.environ.get("LOCALAPPDATA")
        base = Path(local_app) if local_app else (Path.home() / "AppData" / "Local")
        return base / "cchouse" / "bin" / "cchouse.cmd"
    base = Path(home) if home is not None else Path.home()
    return base / ".local" / "bin" / "cchouse"


def _private_tart_bin_dir(home=None):
    base = Path(home) if home is not None else Path.home()
    tools = base / "Library" / "Application Support" / "cchouse" / "bootstrap" / "tools"
    if tools.is_dir():
        for entry in sorted(tools.iterdir(), reverse=True):
            cand = entry / "Contents" / "MacOS"
            if entry.name.startswith("tart-") and (cand / "tart").is_file():
                return cand
    return None


def _install_windows_cli_entry(python_exe=None, repo_root=None, home=None):
    py = str(Path(python_exe or sys.executable).resolve())
    root = str(Path(repo_root or ROOT).resolve())
    entry = cli_entry_path(home=home, platform_name="Windows")
    entry.parent.mkdir(parents=True, exist_ok=True)
    cchouse_py = os.path.join(root, "scripts", "cchouse.py")
    script = (
        "@echo off\r\n"
        "rem Managed by cchouse\r\n"
        "setlocal\r\n"
        "set \"PYTHONUTF8=1\"\r\n"
        f"\"{py}\" -I \"{cchouse_py}\" %*\r\n"
        "exit /b %ERRORLEVEL%\r\n"
    )
    tmp = entry.with_name(entry.name + ".tmp")
    tmp.write_text(script, encoding="utf-8")
    os.replace(tmp, entry)
    path_dirs = [os.path.normcase(os.path.normpath(p)) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    in_path = os.path.normcase(os.path.normpath(str(entry.parent))) in path_dirs
    return entry, in_path


def install_cli_entry(python_exe=None, repo_root=None, home=None, platform_name=None):
    """安装平台命令入口（macOS ~/.local/bin/cchouse、Windows %LOCALAPPDATA%\\cchouse\\bin\\cchouse.cmd）。"""
    sys_name = platform.system() if platform_name is None else platform_name
    if sys_name == "Windows":
        return _install_windows_cli_entry(python_exe=python_exe, repo_root=repo_root, home=home)
    if sys_name != "Darwin":
        return None, False
    base = Path(home) if home is not None else Path.home()
    py = str(Path(python_exe or sys.executable).resolve())
    root = str(Path(repo_root or ROOT).resolve())
    entry = cli_entry_path(base, platform_name="Darwin")
    entry.parent.mkdir(parents=True, exist_ok=True)
    tart_dir = _private_tart_bin_dir(base)
    tart_line = ""
    if tart_dir is not None:
        tart_line = f'export PATH={shlex.quote(str(tart_dir))}:"$PATH"\n'
    script = (
        "#!/bin/bash\n"
        "# Managed by cchouse\n"
        "set -euo pipefail\n"
        'export PYTHONUTF8=1\n'
        f"{tart_line}"
        f'exec {shlex.quote(py)} -I {shlex.quote(os.path.join(root, "scripts", "cchouse.py"))} "$@"\n'
    )
    tmp = entry.with_name(entry.name + ".tmp")
    tmp.write_text(script, encoding="utf-8")
    os.chmod(tmp, 0o755)
    os.replace(tmp, entry)
    path_dirs = [os.path.normpath(os.path.expanduser(p)) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    in_path = os.path.normpath(str(entry.parent)) in path_dirs
    return entry, in_path


def _expected_app_name(tag):
    return f"cchouse ({tag}).app"


def _expected_desktop_name(tag):
    return f"cchouse ({tag})"


def _mac_open_command(tag, state, home):
    """构造 .app 内部调用的 shell 命令（优先使用 ~/.local/bin/cchouse，否则直接调当前 Python + cchouse.py）。"""
    entry = cli_entry_path(home)
    private_dir = state.get("private_dir")
    args = ["open", "--tag", tag]
    if private_dir:
        args += ["--private-dir", str(private_dir)]
    if entry.is_file():
        cmd = [str(entry), *args]
    else:
        cmd = [str(Path(sys.executable).resolve()), "-I", str(ROOT / "scripts" / "cchouse.py"), *args]
    return " ".join(shlex.quote(c) for c in cmd)


def _applescript_escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _mac_create(tag, state, home=None):
    base = Path(home) if home is not None else Path.home()
    apps_dir = base / "Applications"
    desktop_dir = base / "Desktop"
    apps_dir.mkdir(parents=True, exist_ok=True)
    app_path = apps_dir / _expected_app_name(tag)
    if app_path.exists() or app_path.is_symlink():
        if app_path.is_dir() and not app_path.is_symlink():
            shutil.rmtree(app_path)
        else:
            app_path.unlink()

    shell_cmd = _mac_open_command(tag, state, base)
    # 保证 PATH 包含 ~/.local/bin 和 /opt/homebrew/bin，避免 Finder 启动时 PATH 过窄
    wrapped_cmd = f'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"; {shell_cmd}'
    as_src = f'do shell script "{_applescript_escape(wrapped_cmd)}"\n'
    subprocess.run(
        ["osacompile", "-o", str(app_path), "-e", as_src],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    paths = [str(app_path)]

    if desktop_dir.is_dir():
        alias_path = desktop_dir / _expected_desktop_name(tag)
        if alias_path.exists() or alias_path.is_symlink():
            if alias_path.is_dir() and not alias_path.is_symlink():
                shutil.rmtree(alias_path)
            else:
                alias_path.unlink()
        # 优先用 Finder 建替身；若无桌面/Finder 权限（如无头隔离测试），退回到软链接
        alias_ok = False
        try:
            as_alias = (
                f'tell application "Finder" to make alias file to '
                f'(POSIX file "{_applescript_escape(str(app_path))}") '
                f'at (POSIX file "{_applescript_escape(str(desktop_dir))}") '
                f'with properties {{name:"{_applescript_escape(_expected_desktop_name(tag))}"}}'
            )
            proc = subprocess.run(
                ["osascript", "-e", as_alias],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            alias_ok = proc.returncode == 0 and (alias_path.exists() or alias_path.is_symlink())
        except (OSError, subprocess.SubprocessError):
            alias_ok = False
        if not alias_ok:
            os.symlink(str(app_path), str(alias_path))
        paths.append(str(alias_path))

    state["launchers"] = paths
    return paths


def _is_safe_mac_launcher_path(path, tag):
    name = path.name
    return name in (_expected_app_name(tag), _expected_desktop_name(tag), f"{_expected_desktop_name(tag)}.app")


def _mac_remove(tag, state):
    removed = []
    for raw in list(state.get("launchers") or []):
        if not isinstance(raw, str) or not raw:
            continue
        p = Path(raw)
        if not _is_safe_mac_launcher_path(p, tag):
            continue
        if p.is_symlink() or p.is_file():
            p.unlink(missing_ok=True)
            removed.append(str(p))
        elif p.is_dir():
            shutil.rmtree(p)
            removed.append(str(p))
    state["launchers"] = []
    return removed


def _expected_lnk_name(tag):
    return f"cchouse ({tag}).lnk"


def _ps_single_quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def _windows_open_ps_args(tag, state, cmd_path):
    """构造 .lnk 传递给 powershell.exe 的隐藏窗口参数。"""
    parts = [f"& {_ps_single_quote(cmd_path)}", "open", "--tag", _ps_single_quote(tag)]
    private_dir = state.get("private_dir")
    if private_dir:
        parts += ["--private-dir", _ps_single_quote(private_dir)]
    ps_cmd = " ".join(parts)
    escaped = ps_cmd.replace('"', '\\"')
    return f'-NoProfile -WindowStyle Hidden -Command "{escaped}"'


def _windows_shortcut_dirs(home=None):
    """返回 (desktop_dir, start_menu_programs_dir)。"""
    if home is not None:
        base = Path(home)
        return (
            base / "Desktop",
            base / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        )
    user_home = Path.home()
    desktop = user_home / "Desktop"
    appdata = os.environ.get("APPDATA")
    start_menu = (Path(appdata) if appdata else (user_home / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return desktop, start_menu


def _windows_icon_location():
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if not base:
            continue
        for exe in ("VirtualBoxVM.exe", "VirtualBox.exe"):
            cand = Path(base) / "Oracle" / "VirtualBox" / exe
            if cand.is_file():
                return f"{cand},0"
    return "shell32.dll,15"


def _create_windows_lnk_files(specs):
    """用 Windows PowerShell WScript.Shell COM 批量写入 .lnk（经 UTF-8 Base64 环境变量传参，不依赖控制台句柄）。"""
    ps_script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$raw = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($env:CCHOUSE_LNK_JSON_B64))\n"
        "$items = $raw | ConvertFrom-Json\n"
        "$ws = New-Object -ComObject WScript.Shell\n"
        "foreach ($item in @($items)) {\n"
        "  $parent = Split-Path -Parent $item.path\n"
        "  if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }\n"
        "  $tmpLnk = Join-Path ([System.IO.Path]::GetTempPath()) ('cchouse-' + [guid]::NewGuid().ToString('N') + '.lnk')\n"
        "  try {\n"
        "    $s = $ws.CreateShortcut($tmpLnk)\n"
        "    $s.TargetPath = $item.target\n"
        "    $s.Arguments = $item.arguments\n"
        "    $s.WorkingDirectory = $item.workdir\n"
        "    $s.WindowStyle = 7\n"
        "    $s.Description = $item.description\n"
        "    if ($item.icon) { $s.IconLocation = $item.icon }\n"
        "    $s.Save()\n"
        "    Move-Item -LiteralPath $tmpLnk -Destination $item.path -Force\n"
        "  } finally {\n"
        "    if (Test-Path -LiteralPath $tmpLnk) { Remove-Item -LiteralPath $tmpLnk -Force -ErrorAction SilentlyContinue }\n"
        "  }\n"
        "}\n"
    )
    payload_b64 = base64.b64encode(json.dumps(specs, ensure_ascii=False).encode("utf-8")).decode("ascii")
    cmd_b64 = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    env = dict(os.environ, CCHOUSE_LNK_JSON_B64=payload_b64)
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", cmd_b64],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, proc.args, stderr=proc.stderr)


def _windows_create(tag, state, home=None):
    entry, _in_path = install_cli_entry(home=home, platform_name="Windows")
    desktop_dir, start_menu_dir = _windows_shortcut_dirs(home=home)
    lnk_name = _expected_lnk_name(tag)
    ps_exe = os.path.join(
        os.environ.get("WINDIR") or r"C:\Windows",
        "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
    )
    args = _windows_open_ps_args(tag, state, entry)
    icon = _windows_icon_location()
    desc = f"cchouse ({tag})"
    specs = []
    paths = []
    for folder in (desktop_dir, start_menu_dir):
        if folder is None:
            continue
        lnk_path = folder / lnk_name
        specs.append({
            "path": str(lnk_path),
            "target": ps_exe,
            "arguments": args,
            "workdir": str(entry.parent),
            "description": desc,
            "icon": icon,
        })
        paths.append(str(lnk_path))
    if specs:
        _create_windows_lnk_files(specs)
    state["launchers"] = list(paths)
    return list(paths)


def _is_safe_windows_launcher_path(path, tag):
    return path.name.lower() == _expected_lnk_name(tag).lower()


def _windows_remove(tag, state):
    removed = []
    for raw in list(state.get("launchers") or []):
        if not isinstance(raw, str) or not raw:
            continue
        p = Path(raw)
        if not _is_safe_windows_launcher_path(p, tag):
            continue
        if p.is_file() or p.is_symlink():
            p.unlink(missing_ok=True)
            removed.append(str(p))
    state["launchers"] = []
    return removed


def preview(tag, state):
    """返回 state.json 中记录且当前存在的启动图标路径列表（用于 remove 预览）。"""
    found = []
    for raw in state.get("launchers") or []:
        if not isinstance(raw, str) or not raw:
            continue
        p = Path(raw)
        if p.exists() or p.is_symlink():
            found.append(p)
    return found


def create(tag, state, *, platform_name=None, home=None):
    """创建平台桌面启动图标，并将路径写入 state['launchers']。"""
    sys_name = platform.system() if platform_name is None else platform_name
    if sys_name == "Darwin":
        return _mac_create(tag, state, home=home)
    if sys_name == "Windows":
        return _windows_create(tag, state, home=home)
    state.setdefault("launchers", [])
    return []


def remove(tag, state, *, platform_name=None):
    """删除 state['launchers'] 中记录的启动图标。"""
    sys_name = platform.system() if platform_name is None else platform_name
    if sys_name == "Darwin":
        return _mac_remove(tag, state)
    if sys_name == "Windows":
        return _windows_remove(tag, state)
    state["launchers"] = []
    return []
