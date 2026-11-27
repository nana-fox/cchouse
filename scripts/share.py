#!/usr/bin/env python3
"""宿主共享文件夹路径校验、默认路径计算与状态格式化。"""
import ntpath
import os
import platform
import posixpath
import re
from pathlib import Path

GUEST_SHARE_PATH = "/home/claude/shared"
VBOX_SHARE_NAME = "cchouse-shared"
TART_SHARE_NAME = "shared"

FORBIDDEN_HOME_SUBDIRS = frozenset({
    "desktop",
    "documents",
    "downloads",
    "桌面",
    "文档",
    "下载",
})

FORBIDDEN_POSIX_EXACT = frozenset({
    "/",
    "/users",
    "/home",
    "/system",
    "/library",
    "/applications",
    "/volumes",
    "/etc",
    "/var",
    "/usr",
    "/bin",
    "/sbin",
})

FORBIDDEN_WIN_ROOT_SUBDIRS = frozenset({
    "users",
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
})


class SharePathError(ValueError):
    """共享文件夹路径校验失败（中文原因）。"""


def default_share_dir(tag, *, home=None, platform_name=None):
    """默认宿主共享文件夹：Mac ~/cchouse-share/<tag>，Windows %USERPROFILE%\\cchouse-share\\<tag>。"""
    sys_name = platform.system() if platform_name is None else platform_name
    if sys_name == "Windows":
        base = str(home) if home is not None else (os.environ.get("USERPROFILE") or str(Path.home()))
        return ntpath.normpath(ntpath.join(base, "cchouse-share", tag))
    base = Path(home) if home is not None else Path.home()
    return str(base / "cchouse-share" / tag)


def _strip_win_extended_prefix(path_str):
    s = path_str.replace("/", "\\")
    if s[:8].lower() == "\\\\?\\unc\\":
        return "\\\\" + s[8:]
    if re.match(r"^\\\\\?\\[A-Za-z]:\\", s):
        return s[4:]
    return s


def _normalize_windows_path(raw_path, home):
    s = _strip_win_extended_prefix(raw_path.strip())
    if s == "~" or s.startswith("~\\"):
        s = ntpath.join(home, s[2:]) if len(s) > 2 else home
    norm = ntpath.normpath(s)
    # 兼容 "C:" 这类无反斜杠的盘符写法
    if re.fullmatch(r"[A-Za-z]:", norm):
        norm = norm + "\\"
    if not ntpath.isabs(norm):
        norm = ntpath.normpath(ntpath.join(home, norm))
    return norm


def _normalize_posix_path(raw_path, home):
    s = raw_path.strip()
    if s == "~" or s.startswith("~/"):
        s = posixpath.join(home, s[2:]) if len(s) > 2 else home
    if not posixpath.isabs(s):
        s = posixpath.join(os.getcwd(), s)
    return posixpath.normpath(s)


def _check_forbidden_windows(norm_path, norm_home):
    p_low = norm_path.rstrip("\\").lower()
    h_low = norm_home.rstrip("\\").lower()

    if norm_path.startswith("\\\\"):
        raise SharePathError(f"不允许共享网络共享或 UNC 路径：{norm_path}")

    # 任意磁盘根目录（如 C:\, D:\）
    if re.fullmatch(r"[a-z]:", p_low):
        raise SharePathError(f"不允许共享系统盘或磁盘根目录：{norm_path}")

    # C:\Users、C:\Windows 等系统级目录本身
    m = re.fullmatch(r"([a-z]:)\\([^\\]+)", p_low)
    if m and m.group(2) in FORBIDDEN_WIN_ROOT_SUBDIRS:
        raise SharePathError(f"不允许共享系统目录或用户根容器：{norm_path}")

    # 用户家目录本身
    if p_low == h_low:
        raise SharePathError(
            f"不允许直接共享用户家目录本身（{norm_path}），请改用子文件夹（如 %USERPROFILE%\\cchouse-share\\cc01）"
        )

    # Desktop/Documents/Downloads/桌面/文档/下载 这些目录本身（其子目录允许）
    if p_low.startswith(h_low + "\\"):
        rel = p_low[len(h_low) + 1:]
        if "\\" not in rel and rel in FORBIDDEN_HOME_SUBDIRS:
            raise SharePathError(
                f"不允许直接共享个人敏感目录本身（{norm_path}）；允许共享其下的专用子文件夹"
            )


def _check_forbidden_posix(norm_path, norm_home):
    if ":" in norm_path:
        raise SharePathError(
            f"共享文件夹路径不能包含冒号（:）（Mac 下 tart --dir 用冒号分隔参数，Finder 中名称含 / 的文件夹在磁盘上为冒号）：{norm_path}"
        )

    p_clean = "/" if norm_path == "/" else norm_path.rstrip("/")
    h_clean = "/" if norm_home == "/" else norm_home.rstrip("/")
    p_low = p_clean.lower()
    h_low = h_clean.lower()

    if p_clean == "/":
        raise SharePathError("不允许共享系统根目录（/）")

    if p_low in FORBIDDEN_POSIX_EXACT:
        raise SharePathError(f"不允许共享系统目录或用户根容器：{norm_path}")

    if p_low == h_low:
        raise SharePathError(
            f"不允许直接共享用户家目录本身（{norm_path}），请改用子文件夹（如 ~/cchouse-share/cc01）"
        )

    if p_low.startswith(h_low + "/"):
        rel = p_low[len(h_low) + 1:]
        if "/" not in rel and rel in FORBIDDEN_HOME_SUBDIRS:
            raise SharePathError(
                f"不允许直接共享个人敏感目录本身（{norm_path}）；允许共享其下的专用子文件夹"
            )


def _resolve_existing_or_ancestor(path_str):
    """解析路径（含符号链接）；若目标尚不存在，解析其最近已存在祖先并拼接剩余段。"""
    p = Path(path_str)
    tail = []
    cur = p
    while not cur.exists() and not cur.is_symlink():
        parent = cur.parent
        if parent == cur:
            break
        tail.append(cur.name)
        cur = parent
    resolved_base = os.path.realpath(str(cur))
    if tail:
        return os.path.join(resolved_base, *reversed(tail))
    return resolved_base


def validate_share_path(
    raw_path,
    *,
    platform_name=None,
    home=None,
    allow_create=False,
    check_fs=True,
):
    """校验共享文件夹路径并返回规范化后的真实绝对路径；不合法时抛出 SharePathError。"""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise SharePathError("共享文件夹路径不能为空")
    for ch in ("\x00", "\r", "\n"):
        if ch in raw_path:
            raise SharePathError("共享文件夹路径包含非法控制字符")

    sys_name = platform.system() if platform_name is None else platform_name
    is_win = sys_name == "Windows"

    if not is_win and ":" in raw_path:
        raise SharePathError(
            f"共享文件夹路径不能包含冒号（:）（Mac 下 tart --dir 用冒号分隔参数，Finder 中名称含 / 的文件夹在磁盘上为冒号）：{raw_path}"
        )

    if is_win:
        raw_home = str(home) if home is not None else (os.environ.get("USERPROFILE") or str(Path.home()))
        norm_home = _normalize_windows_path(raw_home, raw_home)
        norm_path = _normalize_windows_path(raw_path, norm_home)
        _check_forbidden_windows(norm_path, norm_home)
    else:
        raw_home = str(home) if home is not None else str(Path.home())
        norm_home = _normalize_posix_path(raw_home, raw_home)
        norm_path = _normalize_posix_path(raw_path, norm_home)
        _check_forbidden_posix(norm_path, norm_home)

    if not check_fs:
        return norm_path

    # 只有当平台与当前运行 OS 匹配时才检查真实文件系统与符号链接
    same_os = (is_win and os.name == "nt") or (not is_win and os.name != "nt")
    if not same_os:
        return norm_path

    path_obj = Path(norm_path)
    exists = path_obj.exists()
    is_link = path_obj.is_symlink()

    if not exists and not allow_create:
        if is_link:
            raise SharePathError(f"共享文件夹符号链接指向不存在的目标：{norm_path}")
        raise SharePathError(f"共享文件夹不存在：{norm_path}（请先创建该文件夹）")

    if exists and not path_obj.is_dir():
        raise SharePathError(f"共享路径必须是文件夹，不能是文件：{norm_path}")

    # 解析所有符号链接（包含父目录和目录本身）后再次校验禁止清单
    real_home = _strip_win_extended_prefix(os.path.realpath(norm_home)) if is_win else os.path.realpath(norm_home)
    real_path = _resolve_existing_or_ancestor(norm_path)
    if is_win:
        real_path = _normalize_windows_path(real_path, real_home)
        _check_forbidden_windows(real_path, real_home)
        _check_forbidden_windows(real_path, norm_home)
    else:
        real_path = _normalize_posix_path(real_path, real_home)
        _check_forbidden_posix(real_path, real_home)
        _check_forbidden_posix(real_path, norm_home)

    if os.path.exists(real_path) and not os.path.isdir(real_path):
        raise SharePathError(f"共享路径解析后不是文件夹：{real_path}")

    return real_path


def ensure_share_dir(path_str):
    """创建默认共享目录（POSIX 下显式 0755，避免受 bootstrap umask 077 影响导致客机只读/无权）。"""
    p = Path(path_str)
    existed = p.exists()
    p.mkdir(parents=True, exist_ok=True)
    if os.name != "nt" and not existed:
        try:
            os.chmod(p, 0o755)
        except OSError:
            pass
    return str(p)


def normalize_share_state(state):
    """从 state.json 读取规范化的共享文件夹配置字典。"""
    raw = (state or {}).get("share")
    if not isinstance(raw, dict):
        return {
            "enabled": False,
            "host_path": "",
            "read_only": False,
            "guest_path": GUEST_SHARE_PATH,
        }
    enabled = bool(raw.get("enabled")) and bool(raw.get("host_path"))
    return {
        "enabled": enabled,
        "host_path": str(raw.get("host_path") or "") if enabled else "",
        "read_only": bool(raw.get("read_only")) if enabled else False,
        "guest_path": str(raw.get("guest_path") or GUEST_SHARE_PATH),
    }


def make_share_state(host_path=None, read_only=False, enabled=True):
    if not enabled or not host_path:
        return {
            "enabled": False,
            "host_path": "",
            "read_only": False,
            "guest_path": GUEST_SHARE_PATH,
        }
    return {
        "enabled": True,
        "host_path": str(host_path),
        "read_only": bool(read_only),
        "guest_path": GUEST_SHARE_PATH,
    }


def format_share_status_line(state):
    cfg = normalize_share_state(state)
    if not cfg["enabled"]:
        return "共享文件夹：未开启"
    mode = "只读" if cfg["read_only"] else "读写"
    return f"共享文件夹：{cfg['host_path']}（{mode}）"


def check_vbox_showvminfo_share(lines, expected_share):
    """核对 VBoxManage showvminfo --machinereadable 的共享目录与拖放设置。

    返回 (ok: bool, detail: str)。
    """
    drag_and_drop = None
    names = {}
    paths = {}
    for line in lines:
        s = line.strip()
        m_dnd = re.match(r'^draganddrop="([^"]*)"$', s)
        if m_dnd:
            drag_and_drop = m_dnd.group(1).lower()
            continue
        m_name = re.match(r'^SharedFolderNameMachineMapping(\d+)="([^"]*)"$', s)
        if m_name:
            names[m_name.group(1)] = m_name.group(2)
            continue
        m_path = re.match(r'^SharedFolderPathMachineMapping(\d+)="([^"]*)"$', s)
        if m_path:
            paths[m_path.group(1)] = m_path.group(2).replace("\\\\", "\\")
            continue

    if drag_and_drop is not None and drag_and_drop != "disabled":
        return False, f"VirtualBox 拖放未关闭（draganddrop={drag_and_drop}）"

    mappings = [(names[k], paths.get(k, "")) for k in sorted(names.keys(), key=int)]
    cfg = normalize_share_state({"share": expected_share}) if isinstance(expected_share, dict) else normalize_share_state({})

    if not cfg["enabled"]:
        if mappings:
            return False, f"未开启共享文件夹但存在 VirtualBox 共享映射：{mappings}"
        return True, "未开启共享且无多余映射、拖放已关闭"

    if len(mappings) != 1:
        return False, f"期望恰好 1 个 VirtualBox 共享映射，实际 {len(mappings)} 个：{mappings}"

    got_name, got_path = mappings[0]
    if got_name != VBOX_SHARE_NAME:
        return False, f"VirtualBox 共享名不符（期望 {VBOX_SHARE_NAME}，实际 {got_name}）"

    exp_norm = _strip_win_extended_prefix(cfg["host_path"]).rstrip("\\").lower()
    got_norm = _strip_win_extended_prefix(got_path).rstrip("\\").lower()
    if exp_norm != got_norm:
        return False, f"VirtualBox 共享宿主路径不符（期望 {cfg['host_path']}，实际 {got_path}）"

    return True, "VirtualBox 共享目录与禁用拖放配置一致"
