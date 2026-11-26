#!/usr/bin/env python3
"""cchouse 跨平台环境与配置自检（面向用户与外部 AI Agent，输出脱敏 JSON）。

用法：
  python3 -I scripts/preflight.py [--route auto|macos-tart|tart-linux|virtualbox-linux] [--env <instance.env>] [--state <state.json>]
"""
import argparse
import base64
import ctypes
import datetime
import ipaddress
import json
import math
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import render as render_mod  # noqa: E402

def detect_arch():
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "amd64"
    return m


def find_vboxmanage():
    override = os.environ.get("CCHOUSE_VBOXMANAGE")
    if override and os.path.isfile(override):
        return override
    if platform.system() == "Windows":
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(key)
            if base:
                path = os.path.join(base, "Oracle", "VirtualBox", "VBoxManage.exe")
                if os.path.isfile(path):
                    return path
    return None


def find_tart():
    for cand in (shutil.which("tart"), str(Path.home() / ".local/bin/tart"), "/opt/homebrew/bin/tart"):
        if cand and os.path.isfile(cand):
            return cand
    return None


def detect_ram_gb():
    sys_name = platform.system()
    try:
        if sys_name == "Darwin":
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
            return round(int(out) / (1024 ** 3), 1)
        if sys_name == "Linux":
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return round(kb / (1024 ** 2), 1)
        if sys_name == "Windows":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return round(stat.ullTotalPhys / (1024 ** 3), 1)
    except Exception:
        return None
    return None


def _parse_wrapper_tart_home(tart_bin):
    """当 tart 通过 host/tart-wrapper.sh.example 包装脚本安装时，解析其中 export 的 TART_HOME。"""
    if not tart_bin or not os.path.isfile(tart_bin):
        return None
    try:
        with open(tart_bin, "rb") as f:
            head = f.read(4096)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    text = head.decode("utf-8", errors="ignore")
    for line in text.splitlines():
        m = re.match(r'^\s*(?:export\s+)?TART_HOME=(?:"([^"]+)"|\'([^\']+)\'|([^\s#]+))', line)
        if m:
            raw = m.group(1) or m.group(2) or m.group(3)
            if raw:
                return Path(os.path.expanduser(os.path.expandvars(raw)))
    return None


def tart_home_dir(tart_bin=None):
    env_home = os.environ.get("TART_HOME", "").strip()
    if env_home:
        return Path(os.path.expanduser(env_home))
    wrapper_home = _parse_wrapper_tart_home(tart_bin if tart_bin is not None else find_tart())
    if wrapper_home is not None:
        return wrapper_home
    return Path.home() / ".tart"


def has_tart_cached_image(th=None):
    th = Path(th) if th is not None else tart_home_dir()
    oci_dir = th / "cache" / "OCIs"
    if oci_dir.is_dir():
        for disk in oci_dir.rglob("disk.img"):
            if disk.is_file():
                return True
    vms_dir = th / "vms"
    if vms_dir.is_dir():
        for entry in vms_dir.iterdir():
            name = entry.name.lower()
            if entry.is_dir() and ("vanilla" in name or "base" in name) and (entry / "disk.img").is_file():
                return True
    return False


TART_LINUX_GOLDEN_RE = re.compile(r"^cchouse-debian13-arm64(?:-v\d[0-9a-zA-Z._-]*)?$")


def find_tart_linux_golden_images(th=None):
    """检测本地是否已有可复用的 Tart Debian 13 黄金镜像（匹配正式命名且排除 -building 临时构建机，兼容 tart import）。"""
    th = Path(th) if th is not None else tart_home_dir()
    vms_dir = th / "vms"
    found = []
    if vms_dir.is_dir():
        for entry in sorted(vms_dir.iterdir()):
            if (
                entry.is_dir()
                and TART_LINUX_GOLDEN_RE.match(entry.name)
                and not entry.name.endswith("-building")
                and (entry / "disk.img").is_file()
            ):
                found.append(entry.name)
    return found


# 2026-11-26 只读实测 Mac Tart 完整安装（du，GiB = 1024**3；tart list 的 Size 为四舍五入的整数 GB）：
#   镜像缓存目录 1109405696（1.033 GiB，含 .tvm 与 Claude Code 包）
#   黄金镜像 cchouse-debian13-arm64-v2.0.0-preview.2 2268217344（2.112 GiB，Size 2 GB）
#   客机 2781511680（2.590 GiB，Size 3 GB）
#   干净快照 2674987008（2.491 GiB，Size 3 GB）
#   合计 8834121728 字节 = 8.227 GiB。至少 9 GB 是向上取整；建议 12 GB 再留约 3 GB 给客机磁盘增长。
# 门槛按十进制 GB 向上取整。默认 ~/.tart 与缓存目录同卷。
BYTES_PER_GB = 1_000_000_000
TART_LINUX_DISK_MIN_GB = 9
TART_LINUX_DISK_RECOMMENDED_GB = 12
# Windows 只按 VirtualBox 实测。来源：status-w7.md 2026-11-26 12:01 DISK_MEASURED。
# GTR，VirtualBox 7.2.22，HVCI 开，正式 OVA。NTFS 实际分配：
#   缓存 + 客机 + 干净快照 = 5924945920 字节
#   安装前后该卷可用空间减少 6196707328 字节（约 5.8 GiB，十进制 6.197 GB）
# 与 Mac 相同：按十进制 GB 向上取整（6.197 → 7），建议值再留约 3 GB（10）。
VIRTUALBOX_DISK_MIN_GB = 7
VIRTUALBOX_DISK_RECOMMENDED_GB = 10


def default_install_dir():
    """安装器写入缓存和 Windows 客机的目录。不创建。"""
    sys_name = platform.system()
    if sys_name == "Darwin":
        return Path.home() / "Library" / "Application Support" / "cchouse"
    if sys_name == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else (Path.home() / "AppData" / "Local")) / "cchouse"
    return Path.home() / ".local" / "share" / "cchouse"


def existing_volume_path(path):
    """返回 path，若不存在则返回最近的已存在父目录。不创建任何目录。"""
    cur = Path(path).expanduser()
    while not cur.exists():
        parent = cur.parent
        if parent == cur:
            return Path.home()
        cur = parent
    if cur.is_dir():
        return cur
    return cur.parent


def install_disk_probe_path(route, tart_home=None):
    """安装占用所在卷上的一个已存在目录，供 shutil.disk_usage 使用。"""
    if route in ("macos-tart", "tart-linux"):
        home = tart_home if tart_home is not None else tart_home_dir()
        return existing_volume_path(home)
    return existing_volume_path(default_install_dir())


def disk_requirement_gb(route):
    """返回 (最低 GB, 建议 GB, 是否为估算)。tart-linux 与 Windows 都是实测。macos-tart 沿用单独的 30/50 GB 规则。"""
    if route == "tart-linux":
        return TART_LINUX_DISK_MIN_GB, TART_LINUX_DISK_RECOMMENDED_GB, False
    if route == "virtualbox-linux":
        return VIRTUALBOX_DISK_MIN_GB, VIRTUALBOX_DISK_RECOMMENDED_GB, False
    return None, None, False


def assess_install_disk(free_gb, route):
    """低于实测最低值返回错误，低于建议值只返回警告。"""
    minimum, recommended, _estimate = disk_requirement_gb(route)
    if minimum is None:
        return [], []
    free_display = math.floor(free_gb * 10) / 10
    if free_gb < minimum:
        if route == "tart-linux":
            need = (
                f"完整安装至少需要 {minimum} GB（镜像缓存、黄金镜像、客机和干净快照）。"
                "请清理空间或把 TART_HOME 指到更大的卷。"
            )
        else:
            need = (
                f"完整安装至少需要 {minimum} GB（含下载的镜像）。"
                "请清理该卷空间后再安装。"
            )
        return [(
            f"INSUFFICIENT_DISK: 安装目录所在卷剩余 {free_display:.1f} GB，{need}"
        )], []
    if free_gb < recommended:
        if route == "tart-linux":
            hint = f"建议至少 {recommended} GB，以便客机磁盘继续增长。"
        else:
            hint = f"建议至少 {recommended} GB。"
        return [], [(
            f"LOW_DISK: 安装目录所在卷剩余 {free_display:.1f} GB，可以安装。{hint}"
        )]
    return [], []


def expected_image_for_route(route, manifest_path=None):
    """从 guest-linux/image-manifest.json (schema 2) 解析当前路线期望的黄金镜像元数据。"""
    if route not in ("virtualbox-linux", "tart-linux"):
        return None
    mpath = Path(manifest_path) if manifest_path is not None else (ROOT / "guest-linux" / "image-manifest.json")
    if not mpath.is_file():
        return None
    try:
        data = json.loads(mpath.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("schema_version") != 2:
        return None
    for art in data.get("artifacts", []):
        if isinstance(art, dict) and art.get("route") == route:
            urls = art.get("urls") or []
            return {
                "image_version": data.get("image_version"),
                "arch": art.get("arch"),
                "format": art.get("format"),
                "filename": art.get("filename"),
                "size_bytes": art.get("size_bytes"),
                "sha256": art.get("sha256"),
                "source_commit": art.get("source_commit"),
                "url": urls[0] if urls else None,
            }
    return None


def check_lf_endings(directory):
    bad = []
    if not directory.is_dir():
        return bad
    for p in sorted(directory.iterdir()):
        if not p.is_file():
            continue
        if p.suffix in (".sh", ".conf", ".nft", ".yaml", ".yml", ".service", ".timer", ".json", ".html"):
            raw = p.read_bytes()
            if b"\r\n" in raw:
                bad.append(str(p.relative_to(ROOT)))
    return bad


def _validate_node_fields(env, prefix, errors, warnings):
    k = lambda suffix: f"{prefix}_{suffix}"
    stype = env.get(k("TYPE"), "").strip().lower()
    if stype in render_mod.REJECTED_UDP_TYPES:
        errors.append(f"UDP_PROXY_REJECTED: {k('TYPE')}={stype} 是 UDP 协议，cchouse 默认禁 UDP")
    elif stype not in render_mod.SUPPORTED_SINGLE_TYPES:
        errors.append(f"INVALID_{prefix}_TYPE: 不支持的 {k('TYPE')}={stype or '<空>'}")
    server = env.get(k("SERVER"), "").strip()
    if not server:
        errors.append(f"MISSING_{prefix}_SERVER: 缺少 {k('SERVER')}")
    elif not render_mod.is_ipv4(server):
        pinned = env.get(k("SERVER_IP"), "").strip()
        if not pinned:
            warnings.append(
                f"DOMAIN_NEEDS_PIN: {k('SERVER')} 是域名且未填 {k('SERVER_IP')}；"
                "请先在宿主解析 A 记录填入，或渲染时使用 render.py --resolve"
            )
        elif not render_mod.is_ipv4(pinned):
            errors.append(f"INVALID_{prefix}_SERVER_IP: {k('SERVER_IP')} 不是有效 IPv4")
    port = env.get(k("PORT"), "").strip()
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        errors.append(f"INVALID_{prefix}_PORT: {k('PORT')} 必须是 1-65535 的端口")


def _probe_socks5_connect(proxy_host, proxy_port, user, pwd, target_host, target_port=443, timeout=8):
    try:
        s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    except OSError as exc:
        return False, f"proxy_connect_error({type(exc).__name__})"
    try:
        s.settimeout(timeout)
        if user or pwd:
            s.sendall(b"\x05\x02\x00\x02")
        else:
            s.sendall(b"\x05\x01\x00")
        resp = s.recv(2)
        if len(resp) < 2 or resp[0] != 5:
            return False, "socks5_handshake_failed"
        if resp[1] == 0x02:
            ub, pb = user.encode("utf-8"), pwd.encode("utf-8")
            s.sendall(bytes([1, len(ub)]) + ub + bytes([len(pb)]) + pb)
            auth = s.recv(2)
            if auth != b"\x01\x00":
                return False, "socks5_auth_failed"
        elif resp[1] != 0x00:
            return False, f"socks5_method_rejected(0x{resp[1]:02x})"
        hb = target_host.encode("ascii")
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack("!H", target_port))
        rep = s.recv(10)
        if len(rep) < 2:
            return False, "socks5_empty_reply"
        if rep[1] == 0x00:
            return True, "ok"
        if rep[1] == 0x02:
            return False, "socks5_ruleset_blocked(0x02)"
        return False, f"socks5_connect_failed(0x{rep[1]:02x})"
    except OSError as exc:
        return False, f"connect_error({type(exc).__name__})"
    finally:
        s.close()


def _probe_http_connect(proxy_host, proxy_port, user, pwd, target_host, target_port=443, timeout=8):
    try:
        s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    except OSError as exc:
        return False, f"proxy_connect_error({type(exc).__name__})"
    try:
        s.settimeout(timeout)
        headers = [f"CONNECT {target_host}:{target_port} HTTP/1.1", f"Host: {target_host}:{target_port}"]
        if user or pwd:
            token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
            headers.append(f"Proxy-Authorization: Basic {token}")
        req = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii")
        s.sendall(req)
        raw = s.recv(1024).decode("latin1", errors="ignore")
        first = raw.splitlines()[0] if raw else ""
        parts = first.split()
        if len(parts) >= 2 and parts[1] == "200":
            return True, "ok"
        code = parts[1] if len(parts) >= 2 else "empty"
        return False, f"http_connect_rejected({code})"
    except OSError as exc:
        return False, f"connect_error({type(exc).__name__})"
    finally:
        s.close()


def check_stripe_reachability(env_path, timeout=8):
    """在宿主机通过 socks5/http 节点探测 Stripe 核心域名可达性（绝不输出凭据）。"""
    try:
        env = render_mod.load_env(env_path)
    except SystemExit:
        return None
    guest_os = env.get("GUEST_OS", "macos").strip().lower()
    exit_mode = env.get("EXIT_MODE", "chain").strip().lower()
    if exit_mode != "single":
        return {"skipped": True, "reason": "chain_mode_uses_guest_verify"}
    stripe_mode = render_mod.effective_stripe_mode(env, guest_os, exit_mode)
    prefix = "STRIPE_NODE" if stripe_mode == "node" else "SINGLE"
    stype = env.get(f"{prefix}_TYPE", "").strip().lower()
    if stype not in ("socks5", "http"):
        return {
            "skipped": True,
            "target": prefix,
            "protocol": stype,
            "reason": "protocol_requires_guest_verify",
        }
    host = (env.get(f"{prefix}_SERVER_IP", "").strip() or env.get(f"{prefix}_SERVER", "").strip())
    port_s = env.get(f"{prefix}_PORT", "").strip()
    if not host or not port_s.isdigit():
        return {"skipped": True, "target": prefix, "reason": "invalid_host_or_port"}
    port = int(port_s)
    user = env.get(f"{prefix}_USER", "")
    pwd = env.get(f"{prefix}_PASS", "")
    probe_fn = _probe_socks5_connect if stype == "socks5" else _probe_http_connect
    domains = {}
    all_ok = True
    for dom in ("js.stripe.com", "api.stripe.com"):
        ok_dom, reason = probe_fn(host, port, user, pwd, dom, 443, timeout=timeout)
        domains[dom] = {"reachable": ok_dom, "detail": reason}
        if not ok_dom:
            all_ok = False
    return {
        "skipped": False,
        "target": prefix,
        "stripe_mode": stripe_mode,
        "reachable": all_ok,
        "domains": domains,
    }


def validate_env_file(env_path, route):
    errors, warnings = [], []
    try:
        env = render_mod.load_env(env_path)
    except SystemExit as exc:
        return [str(exc)], warnings

    if not env.get("TAG"):
        errors.append("MISSING_TAG: instance.env 缺少 TAG")
    guest_os = env.get("GUEST_OS", "macos").strip().lower()
    exit_mode = env.get("EXIT_MODE", "chain").strip().lower()
    stripe_mode = render_mod.effective_stripe_mode(env, guest_os, exit_mode)

    if route in ("virtualbox-linux", "tart-linux") and guest_os != "linux":
        errors.append(f"ROUTE_OS_MISMATCH: {route} 路线要求 GUEST_OS=linux，当前为 {guest_os}")
    if route == "macos-tart" and guest_os != "macos":
        errors.append(f"ROUTE_OS_MISMATCH: macos-tart 路线要求 GUEST_OS=macos，当前为 {guest_os}")

    if exit_mode not in ("single", "chain"):
        errors.append(f"INVALID_EXIT_MODE: EXIT_MODE={exit_mode}，仅支持 single 或 chain")
    elif exit_mode == "single":
        if guest_os != "linux":
            errors.append("UNSUPPORTED_SINGLE_ON_MACOS: v1 macOS 模板当前仅支持 EXIT_MODE=chain")
        if stripe_mode not in ("off", "node"):
            errors.append("INVALID_STRIPE_MODE: EXIT_MODE=single 时 STRIPE_EXIT_MODE 仅支持 off 或 node")
        _validate_node_fields(env, "SINGLE", errors, warnings)
        if stripe_mode == "node":
            _validate_node_fields(env, "STRIPE_NODE", errors, warnings)
        elif env.get("STRIPE_NODE_SERVER", "").strip():
            warnings.append(
                "STRIPE_NODE_IGNORED: 已填 STRIPE_NODE_SERVER 但 STRIPE_EXIT_MODE=off，"
                "Stripe 备用节点不会生效；如需启用请设 STRIPE_EXIT_MODE=node"
            )
    else:
        if stripe_mode not in ("off", "front", "tmh-chain"):
            errors.append(f"INVALID_STRIPE_MODE: {stripe_mode}")
        if guest_os == "macos" and stripe_mode == "off":
            errors.append("INVALID_STRIPE_MODE: v1 macOS 仅支持 front 或 tmh-chain")

    for req in ("TIMEZONE", "EXPECTED_EGRESS_IP", "EXPECTED_EGRESS_ASN", "HOME_LAN_CIDR"):
        if not env.get(req):
            warnings.append(f"EMPTY_FIELD: {req} 为空（验收前必须填写）")

    for cidr_key in ("HOME_LAN_CIDR", "VMNET_CIDR"):
        val = env.get(cidr_key, "").strip()
        if val:
            try:
                net = ipaddress.ip_network(val, strict=False)
                if net.version != 4:
                    errors.append(f"INVALID_CIDR: {cidr_key}={val} 仅支持 IPv4 CIDR")
            except ValueError:
                errors.append(f"INVALID_CIDR: {cidr_key}={val} 不是有效 CIDR")

    if guest_os == "linux" and "GUEST_KEYBOARD" in env:
        kb = env.get("GUEST_KEYBOARD", "").strip()
        if not re.fullmatch(r"[a-z]{2,3}", kb):
            errors.append(
                f"INVALID_GUEST_KEYBOARD: GUEST_KEYBOARD={kb or '<空>'} 仅支持 2-3 位小写 XKB 布局代码（如 us/gb/de/jp）"
            )

    return errors, warnings


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="cchouse preflight checker")
    parser.add_argument("--route", choices=("auto", "macos-tart", "tart-linux", "virtualbox-linux"), default="auto")
    parser.add_argument("--env", dest="env_path", default=None)
    parser.add_argument("--state", dest="state_path", default=None)
    parser.add_argument("--set-step", dest="set_step", default=None)
    parser.add_argument("--set-status", dest="set_status", default=None)
    parser.add_argument("--next-action", dest="set_next_action", default=None)
    parser.add_argument("--check-stripe", action="store_true", help="在宿主机探测单节点代理是否可连接 Stripe")
    args = parser.parse_args()

    sys_name = platform.system()
    arch = detect_arch()
    ram_gb = detect_ram_gb()
    tart_path = find_tart()

    if args.route == "auto":
        if sys_name == "Darwin" and arch == "arm64":
            env_guest_os = ""
            if args.env_path and os.path.isfile(args.env_path):
                try:
                    env_guest_os = render_mod.load_env(args.env_path).get("GUEST_OS", "").strip().lower()
                except SystemExit:
                    env_guest_os = ""
            route = "tart-linux" if env_guest_os == "linux" else "macos-tart"
        else:
            route = "virtualbox-linux"
    else:
        route = args.route

    resolved_tart_home = tart_home_dir(tart_path) if route in ("macos-tart", "tart-linux") else None
    disk_probe_path = install_disk_probe_path(route, resolved_tart_home)
    # The legacy macOS guest keeps its existing 30/50 GiB rules.
    disk_unit = 1024 ** 3 if route == "macos-tart" else BYTES_PER_GB
    disk_gb = shutil.disk_usage(disk_probe_path).free / disk_unit
    disk_min_gb, disk_recommended_gb, disk_estimate = disk_requirement_gb(route)

    errors = []
    warnings = []
    status = "PREFLIGHT_OK"
    next_action = "proceed_to_render"

    if ram_gb is not None and ram_gb < 16:
        warnings.append(f"LOW_RAM: 当前内存 {ram_gb} GB，建议 ≥ 16 GB")

    crlf_files = check_lf_endings(ROOT / "guest-linux")
    if crlf_files:
        errors.append(f"CRLF_DETECTED: 以下 Linux 模板含 CRLF 换行：{', '.join(crlf_files)}")

    if route == "macos-tart":
        if sys_name != "Darwin" or arch != "arm64":
            errors.append("TART_REQUIRES_APPLE_SILICON: macos-tart 仅支持 Apple Silicon macOS")
        if not tart_path:
            status = "HUMAN_WAIT"
            errors.append("MISSING_TART: 未检测到 tart，请先执行 brew install cirruslabs/cli/tart")
            next_action = "install_tart"
        tart_cached = has_tart_cached_image(resolved_tart_home)
        if disk_gb < 30 and not tart_cached:
            status = "HUMAN_WAIT"
            errors.append(
                f"INSUFFICIENT_DISK: Tart 目标卷剩余 {disk_gb:.1f} GB 且本地无缓存底包（拉取 macOS 镜像需 ≥ 30 GB），"
                "请清理空间或将 TART_HOME 指向外置 SSD"
            )
            next_action = "free_disk_or_set_tart_home"
        elif disk_gb < 50:
            if tart_cached:
                warnings.append(f"LOW_DISK: Tart 目标卷剩余 {disk_gb:.1f} GB，建议 ≥ 50 GB（检测到本地缓存底包，可基于 CoW 克隆继续）")
            else:
                warnings.append(f"LOW_DISK: Tart 目标卷剩余 {disk_gb:.1f} GB，建议 ≥ 50 GB（首次拉取镜像空间较紧张，建议预留余量或使用外置 TART_HOME）")
    elif route == "tart-linux":
        if sys_name != "Darwin" or arch != "arm64":
            errors.append("TART_REQUIRES_APPLE_SILICON: tart-linux 仅支持 Apple Silicon macOS")
        if not tart_path:
            status = "HUMAN_WAIT"
            errors.append("MISSING_TART: 未检测到 tart，请先执行 brew install cirruslabs/cli/tart")
            next_action = "install_tart"
    disk_errors, disk_warnings = assess_install_disk(disk_gb, route)
    errors.extend(disk_errors)
    warnings.extend(disk_warnings)
    if disk_errors and status == "PREFLIGHT_OK":
        status = "PREFLIGHT_FAILED"
        next_action = "free_disk"

    stripe_check = None
    if args.env_path:
        env_errors, env_warnings = validate_env_file(args.env_path, route)
        if args.check_stripe and not env_errors:
            stripe_check = check_stripe_reachability(args.env_path)
            if stripe_check and not stripe_check.get("skipped") and not stripe_check.get("reachable"):
                details = ", ".join(
                    f"{d}:{v['detail']}" for d, v in stripe_check.get("domains", {}).items() if not v.get("reachable")
                )
                # 只提示不拦截：Stripe 仅影响 claude.ai 网页付款，不影响已订阅用户正常使用
                if stripe_check.get("stripe_mode") == "node":
                    env_warnings.append(
                        f"STRIPE_NODE_UNREACHABLE: 配置的 STRIPE_NODE 无法连接 Stripe（{details}），claude.ai 网页付款会失败，可更换 Stripe 专用节点"
                    )
                else:
                    env_warnings.append(
                        f"STRIPE_UNREACHABLE: 当前单节点无法连接 Stripe（{details}），claude.ai 付款会失败；"
                        "建议配置 Stripe 备用节点（STRIPE_EXIT_MODE=node + STRIPE_NODE_*）或改链式（EXIT_MODE=chain）"
                    )
        errors.extend(env_errors)
        warnings.extend(env_warnings)
        if env_errors and status != "HUMAN_WAIT":
            status = "ENV_INVALID"
            next_action = "fix_instance_env"
    elif not errors:
        if args.route == "auto" and sys_name == "Darwin" and arch == "arm64":
            next_action = "choose_route"
        else:
            next_action = "confirm_route_and_fill_instance_env"

    ok = len(errors) == 0
    if not ok and status == "PREFLIGHT_OK":
        status = "PREFLIGHT_FAILED"

    if args.set_status:
        status = args.set_status
    if args.set_next_action:
        next_action = args.set_next_action

    tart_linux_golden = find_tart_linux_golden_images(resolved_tart_home) if route == "tart-linux" else []
    expected_image = expected_image_for_route(route)
    result = {
        "ok": ok,
        "status": status,
        "host": {
            "os": sys_name,
            "arch": arch,
            "ram_gb": ram_gb,
            "disk_free_gb": round(disk_gb, 1),
            "disk_min_gb": disk_min_gb,
            "disk_recommended_gb": disk_recommended_gb,
            "disk_estimate": disk_estimate,
            "python": platform.python_version(),
            "tart_found": bool(tart_path),
            "tart_home": str(resolved_tart_home) if resolved_tart_home is not None else None,
            "vboxmanage_found": bool(find_vboxmanage()),
            "tart_linux_golden_images": tart_linux_golden,
            "expected_image": expected_image,
        },
        "route": route,
        "stripe_check": stripe_check,
        "errors": errors,
        "warnings": warnings,
        "next_action": next_action,
    }

    if args.state_path:
        state_file = Path(args.state_path)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(state_file.parent, 0o700)
        prev = {}
        if state_file.is_file():
            try:
                prev = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                prev = {}
        completed = list(prev.get("completed_steps", []))
        cur_step = args.set_step or (
            prev.get("step", "step0_preflight")
            if (ok and prev.get("step") not in (None, "step0_preflight"))
            else "step0_preflight"
        )
        if ok and cur_step not in completed and status != "HUMAN_WAIT":
            completed.append(cur_step)
        state_payload = {
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "step": cur_step,
            "status": status,
            "route": route,
            "ok": ok,
            "completed_steps": completed,
            "next_action": next_action,
        }
        state_file.write_text(json.dumps(state_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(state_file, 0o600)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
