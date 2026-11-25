#!/usr/bin/env python3
"""cchouse 跨平台环境与配置自检（面向用户与外部 AI Agent，输出脱敏 JSON）。

用法：
  python3 -I scripts/preflight.py [--route auto|macos-tart|vmware-linux] [--env <instance.env>] [--state <state.json>]
"""
import argparse
import ctypes
import datetime
import json
import os
import platform
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


def tart_home_dir():
    env_home = os.environ.get("TART_HOME", "").strip()
    if env_home and os.path.isdir(env_home):
        return Path(env_home)
    return Path.home() / ".tart"


def has_tart_cached_image():
    th = tart_home_dir()
    for sub in ("cache/OCIs", "vms"):
        d = th / sub
        if d.is_dir() and any(d.iterdir()):
            return True
    return False


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

    if route == "vmware-linux" and guest_os != "linux":
        errors.append(f"ROUTE_OS_MISMATCH: vmware-linux 路线要求 GUEST_OS=linux，当前为 {guest_os}")
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

    return errors, warnings


def main():
    parser = argparse.ArgumentParser(description="cchouse preflight checker")
    parser.add_argument("--route", choices=("auto", "macos-tart", "vmware-linux"), default="auto")
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
            route = "macos-tart"
        else:
            route = "vmware-linux"
    else:
        route = args.route

    disk_probe_path = tart_home_dir() if (route == "macos-tart" and tart_home_dir().exists()) else ROOT
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
        tart_cached = has_tart_cached_image()
        if disk_gb < 30 and not tart_cached:
            status = "HUMAN_WAIT"
            errors.append(
                f"INSUFFICIENT_DISK: Tart 目标卷剩余 {disk_gb} GB 且本地无缓存底包（拉取 macOS 镜像需 ≥ 30 GB），"
                "请清理空间或将 TART_HOME 指向外置 SSD"
            )
            next_action = "free_disk_or_set_tart_home"
        elif disk_gb < 50:
            warnings.append(f"LOW_DISK: Tart 目标卷剩余 {disk_gb} GB，建议 ≥ 50 GB（本地有缓存底包可继续）")
    elif route == "vmware-linux":
        if sys_name == "Darwin" and arch == "arm64":
            warnings.append(
                "ARM64_VMWARE_NOTE: v2.0 黄金镜像优先提供 amd64（Windows/Intel Mac）；"
                "Apple Silicon Mac 建议优先使用 macos-tart 路线"
            )
        if not vmrun_path:
            status = "HUMAN_WAIT"
            errors.append("MISSING_VMWARE: 未检测到 vmrun，请人工安装免费版 VMware Workstation/Fusion Pro")
            next_action = "install_vmware_human_wait"
        if disk_gb < 25:
            warnings.append(f"LOW_DISK: 当前可用磁盘 {disk_gb} GB，Debian 13 客机建议 ≥ 25 GB")

    if args.env_path:
        env_errors, env_warnings = validate_env_file(args.env_path, route)
        errors.extend(env_errors)
        warnings.extend(env_warnings)
        if env_errors and status != "HUMAN_WAIT":
            status = "ENV_INVALID"
            next_action = "fix_instance_env"
    elif not errors:
        next_action = "confirm_route_and_fill_instance_env"

    ok = len(errors) == 0
    if not ok and status == "PREFLIGHT_OK":
        status = "PREFLIGHT_FAILED"

    if args.set_status:
        status = args.set_status
    if args.set_next_action:
        next_action = args.set_next_action

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
            "vmrun_found": bool(vmrun_path),
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
