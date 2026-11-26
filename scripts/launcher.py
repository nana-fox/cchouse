#!/usr/bin/env python3
"""cchouse 桌面快捷启动图标与命令入口管理。

按平台分派：
  - macOS (_mac): osacompile 生成 ~/Applications/cchouse (<tag>).app（调用 cchouse open --tag <tag>，不弹终端）
                  + 桌面替身 ~/Desktop/cchouse (<tag>)。
  - Windows (_windows): 预留给 Windows 分支（1B）实现 .lnk 快捷方式。

所有创建的图标路径记录在 state["launchers"]（字符串列表），cchouse remove 预览并清理。
"""
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def cli_entry_path(home=None):
    """返回 macOS 命令入口路径 ~/.local/bin/cchouse。"""
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


def install_cli_entry(python_exe=None, repo_root=None, home=None):
    """在 macOS 安装 ~/.local/bin/cchouse 命令入口。返回 (入口路径, 是否已在 PATH 中)。"""
    if platform.system() != "Darwin":
        return None, False
    base = Path(home) if home is not None else Path.home()
    py = str(Path(python_exe or sys.executable).resolve())
    root = str(Path(repo_root or ROOT).resolve())
    entry = cli_entry_path(base)
    entry.parent.mkdir(parents=True, exist_ok=True)
    tart_dir = _private_tart_bin_dir(base)
    tart_line = ""
    if tart_dir is not None:
        tart_line = f'command -v tart >/dev/null 2>&1 || export PATH={shlex.quote(str(tart_dir))}:"$PATH"\n'
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


def _windows_create(tag, state):
    raise NotImplementedError("Windows launcher creation is implemented by 1B")


def _windows_remove(tag, state):
    raise NotImplementedError("Windows launcher removal is implemented by 1B")


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
        return _windows_create(tag, state)
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
