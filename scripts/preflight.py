#!/usr/bin/env python3
"""cchouse 跨平台环境与配置自检（面向用户与外部 AI Agent，输出脱敏 JSON）。

用法：
  python3 -I scripts/preflight.py [--route auto|macos-tart|tart-linux|vmware-linux] [--env <instance.env>] [--state <state.json>]
"""
import argparse
import ctypes
import datetime
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import render as render_mod  # noqa: E402

VMRUN_CANDIDATES = [
    r"C:\Program Files\VMware\VMware Workstation\vmrun.exe",
    r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe",
    "/Applications/VMware Fusion.app/Contents/Library/vmrun",
    "/usr/bin/vmrun",
]


def detect_arch():
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "amd64"
    return m


def find_vmrun():
    found = shutil.which("vmrun") or shutil.which("vmrun.exe")
    if found:
        return found
    for p in VMRUN_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def detect_vmnet8_cidr():
    """在 Windows (Workstation) 或 macOS (Fusion) 上尽力探测 VMware NAT (vmnet8) 网段。"""
    sys_name = platform.system()
    try:
        if sys_name == "Windows":
            ps = (
                "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue "
                "| Where-Object { $_.InterfaceAlias -like '*VMnet8*' } "
                "| Select-Object -First 1 "
                "| ForEach-Object { \"$($_.IPAddress)/$($_.PrefixLength)\" }"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps], text=True, timeout=15
            ).strip()
            if out and "/" in out:
                return str(ipaddress.ip_network(out, strict=False))
        elif sys_name == "Darwin":
            net_cfg = Path("/Library/Preferences/VMware Fusion/networking")
            if net_cfg.is_file():
                text = net_cfg.read_text(encoding="utf-8", errors="ignore")
                m_sub = re.search(r"VNET_8_HOSTONLY_SUBNET\s+(\d+\.\d+\.\d+\.\d+)", text)
                m_mask = re.search(r"VNET_8_HOSTONLY_NETMASK\s+(\d+\.\d+\.\d+\.\d+)", text)
                if m_sub and m_mask:
                    return str(ipaddress.ip_network(f"{m_sub.group(1)}/{m_mask.group(1)}", strict=False))
    except Exception:
        return None
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
    """检测本地是否已有可复用的 Tart Debian 13 黄金镜像（仅精确匹配默认名或 -v* 发布标签，避免误匹配克隆测试机）。"""
    th = Path(th) if th is not None else tart_home_dir()
    vms_dir = th / "vms"
    found = []
    if vms_dir.is_dir():
        for entry in sorted(vms_dir.iterdir()):
            if entry.is_dir() and TART_LINUX_GOLDEN_RE.match(entry.name) and (entry / "disk.img").is_file():
                found.append(entry.name)
    return found


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
    default_stripe = "off" if (guest_os == "linux" and exit_mode == "single") else "front"
    stripe_mode = env.get("STRIPE_EXIT_MODE", default_stripe).strip().lower()

    if route in ("vmware-linux", "tart-linux") and guest_os != "linux":
        errors.append(f"ROUTE_OS_MISMATCH: {route} 路线要求 GUEST_OS=linux，当前为 {guest_os}")
    if route == "macos-tart" and guest_os != "macos":
        errors.append(f"ROUTE_OS_MISMATCH: macos-tart 路线要求 GUEST_OS=macos，当前为 {guest_os}")

    if exit_mode not in ("single", "chain"):
        errors.append(f"INVALID_EXIT_MODE: EXIT_MODE={exit_mode}，仅支持 single 或 chain")
    elif exit_mode == "single":
        if guest_os != "linux":
            errors.append("UNSUPPORTED_SINGLE_ON_MACOS: v1 macOS 模板当前仅支持 EXIT_MODE=chain")
        if stripe_mode != "off":
            errors.append("INVALID_STRIPE_MODE: EXIT_MODE=single 时 STRIPE_EXIT_MODE 必须为 off")
        stype = env.get("SINGLE_TYPE", "").strip().lower()
        if stype in render_mod.REJECTED_UDP_TYPES:
            errors.append(f"UDP_PROXY_REJECTED: SINGLE_TYPE={stype} 是 UDP 协议，cchouse 默认禁 UDP")
        elif stype not in render_mod.SUPPORTED_SINGLE_TYPES:
            errors.append(f"INVALID_SINGLE_TYPE: 不支持的 SINGLE_TYPE={stype or '<空>'}")
        server = env.get("SINGLE_SERVER", "").strip()
        if not server:
            errors.append("MISSING_SINGLE_SERVER: 缺少 SINGLE_SERVER")
        elif not render_mod.is_ipv4(server):
            pinned = env.get("SINGLE_SERVER_IP", "").strip()
            if not pinned:
                warnings.append(
                    "DOMAIN_NEEDS_PIN: SINGLE_SERVER 是域名且未填 SINGLE_SERVER_IP；"
                    "请先在宿主解析 A 记录填入，或渲染时使用 render.py --resolve"
                )
            elif not render_mod.is_ipv4(pinned):
                errors.append("INVALID_SINGLE_SERVER_IP: SINGLE_SERVER_IP 不是有效 IPv4")
        port = env.get("SINGLE_PORT", "").strip()
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            errors.append("INVALID_SINGLE_PORT: SINGLE_PORT 必须是 1-65535 的端口")
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

    if guest_os == "linux":
        kb = env.get("GUEST_KEYBOARD", "").strip()
        if kb and not re.fullmatch(r"[a-z]{2,3}", kb):
            errors.append(f"INVALID_GUEST_KEYBOARD: GUEST_KEYBOARD={kb} 仅支持 2-3 位小写 XKB 布局代码（如 us/gb/de/jp）")

    return errors, warnings


def main():
    parser = argparse.ArgumentParser(description="cchouse preflight checker")
    parser.add_argument("--route", choices=("auto", "macos-tart", "tart-linux", "vmware-linux"), default="auto")
    parser.add_argument("--env", dest="env_path", default=None)
    parser.add_argument("--state", dest="state_path", default=None)
    parser.add_argument("--set-step", dest="set_step", default=None)
    parser.add_argument("--set-status", dest="set_status", default=None)
    parser.add_argument("--next-action", dest="set_next_action", default=None)
    args = parser.parse_args()

    sys_name = platform.system()
    arch = detect_arch()
    ram_gb = detect_ram_gb()
    tart_path = find_tart()
    vmrun_path = find_vmrun()

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
            route = "vmware-linux"
    else:
        route = args.route

    resolved_tart_home = tart_home_dir(tart_path) if route in ("macos-tart", "tart-linux") else None
    disk_probe_path = resolved_tart_home if (resolved_tart_home is not None and resolved_tart_home.exists()) else ROOT
    disk_gb = round(shutil.disk_usage(disk_probe_path).free / (1024 ** 3), 1)

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
                f"INSUFFICIENT_DISK: Tart 目标卷剩余 {disk_gb} GB 且本地无缓存底包（拉取 macOS 镜像需 ≥ 30 GB），"
                "请清理空间或将 TART_HOME 指向外置 SSD"
            )
            next_action = "free_disk_or_set_tart_home"
        elif disk_gb < 50:
            if tart_cached:
                warnings.append(f"LOW_DISK: Tart 目标卷剩余 {disk_gb} GB，建议 ≥ 50 GB（检测到本地缓存底包，可基于 CoW 克隆继续）")
            else:
                warnings.append(f"LOW_DISK: Tart 目标卷剩余 {disk_gb} GB，建议 ≥ 50 GB（首次拉取镜像空间较紧张，建议预留余量或使用外置 TART_HOME）")
    elif route == "tart-linux":
        if sys_name != "Darwin" or arch != "arm64":
            errors.append("TART_REQUIRES_APPLE_SILICON: tart-linux 仅支持 Apple Silicon macOS")
        if not tart_path:
            status = "HUMAN_WAIT"
            errors.append("MISSING_TART: 未检测到 tart，请先执行 brew install cirruslabs/cli/tart")
            next_action = "install_tart"
        if disk_gb < 15:
            warnings.append(f"LOW_DISK: Tart 目标卷剩余 {disk_gb} GB，Debian 13 arm64 客机建议 ≥ 15 GB")
    elif route == "vmware-linux":
        if sys_name == "Darwin" and arch == "arm64":
            warnings.append(
                "ARM64_VMWARE_NOTE: vmware-linux 黄金镜像为 amd64（Windows/Intel Mac）；"
                "Apple Silicon Mac 建议使用 tart-linux（轻量）或 macos-tart（高拟真）路线"
            )
        if not vmrun_path:
            status = "HUMAN_WAIT"
            errors.append("MISSING_VMWARE: 未检测到 vmrun，请人工安装免费版 VMware Workstation/Fusion Pro")
            next_action = "install_vmware_human_wait"
        if disk_gb < 25:
            warnings.append(f"LOW_DISK: 当前可用磁盘 {disk_gb} GB，Debian 13 客机建议 ≥ 25 GB")

    detected_vmnet = detect_vmnet8_cidr() if route == "vmware-linux" else None
    if args.env_path:
        env_errors, env_warnings = validate_env_file(args.env_path, route)
        if route == "vmware-linux" and detected_vmnet:
            try:
                env_vmnet = render_mod.load_env(args.env_path).get("VMNET_CIDR", "").strip()
                if env_vmnet:
                    env_net = str(ipaddress.ip_network(env_vmnet, strict=False))
                    if env_net != detected_vmnet:
                        env_errors.append(
                            f"VMNET_MISMATCH: instance.env 中 VMNET_CIDR={env_net} 与本机 VMnet8 实际网段 {detected_vmnet} 不一致，"
                            "请修改后再渲染，否则 firstboot 会锁死宿主 SSH"
                        )
            except Exception:
                pass
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
    result = {
        "ok": ok,
        "status": status,
        "host": {
            "os": sys_name,
            "arch": arch,
            "ram_gb": ram_gb,
            "disk_free_gb": disk_gb,
            "python": platform.python_version(),
            "tart_found": bool(tart_path),
            "tart_home": str(resolved_tart_home) if resolved_tart_home is not None else None,
            "vmrun_found": bool(vmrun_path),
            "detected_vmnet_cidr": detected_vmnet,
            "tart_linux_golden_images": tart_linux_golden,
        },
        "route": route,
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
