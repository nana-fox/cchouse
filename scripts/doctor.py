#!/usr/bin/env python3
"""cchouse 跨平台脱敏诊断包导出工具（供用户出错时一键导出给维护者或 AI 排错）。

用法：
  python3 -I scripts/doctor.py --env <instance.env> [--vm-ssh] [--out <doctor.zip>]

红线：
  - 任何凭据（密码、UUID、私钥、节点地址、用户名、token 等）都不得进入 zip。
  - 所有写入 zip 的文本与 JSON 均经过统一脱敏管道。
  - 客机不可达时依然正常出包，并在 manifest.json 中明确标明缺失项与原因。
"""
import argparse
import base64
import datetime
import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import preflight as preflight_mod  # noqa: E402
import render as render_mod  # noqa: E402
from cchouse import image_identity  # noqa: E402

# 明确非敏感、允许在 env-summary.json 中保留的结构性配置键（白名单）
SAFE_ENV_SUMMARY_KEYS = (
    "GUEST_OS",
    "EXIT_MODE",
    "STRIPE_EXIT_MODE",
    "TAG",
    "VM_NAME",
    "GUEST_IF",
    "MIHOMO_USER",
    "NETWORK_MODE",
    "BRIDGE_IF",
    "TIMEZONE",
    "GUEST_LOCALE",
    "GUEST_KEYBOARD",
    "EXPECTED_EGRESS_LOC",
    "EXPECTED_EGRESS_ASN",
    "SINGLE_TYPE",
    "SINGLE_NETWORK",
    "SINGLE_FINGERPRINT",
    "SINGLE_TLS",
    "SINGLE_VIA_TYPE",
    "SINGLE_VIA_NETWORK",
    "SINGLE_VIA_FINGERPRINT",
    "SINGLE_VIA_TLS",
    "STRIPE_NODE_TYPE",
    "STRIPE_NODE_NETWORK",
    "STRIPE_NODE_FINGERPRINT",
    "STRIPE_NODE_TLS",
    "FRONT_FINGERPRINT",
    "NODE_LABEL",
)

# 明确不属于凭据的键（防止如 MIHOMO_USER=mihomo 误脱敏 cchouse-mihomo.service）
NON_SECRET_ENV_KEYS = frozenset(
    {
        "GUEST_OS",
        "EXIT_MODE",
        "STRIPE_EXIT_MODE",
        "TAG",
        "TAG_UPPER",
        "VM_NAME",
        "CLEAN_SNAPSHOT",
        "GUEST_IF",
        "MIHOMO_USER",
        "NETWORK_MODE",
        "BRIDGE_IF",
        "TIMEZONE",
        "GUEST_LOCALE",
        "GUEST_KEYBOARD",
        "GUEST_HOSTNAME",
        "EXPECTED_EGRESS_LOC",
        "EXPECTED_EGRESS_ASN",
        "SINGLE_TYPE",
        "SINGLE_NETWORK",
        "SINGLE_FINGERPRINT",
        "SINGLE_TLS",
        "SINGLE_CIPHER",
        "SINGLE_FLOW",
        "SINGLE_VIA_TYPE",
        "SINGLE_VIA_NETWORK",
        "SINGLE_VIA_FINGERPRINT",
        "SINGLE_VIA_TLS",
        "SINGLE_VIA_CIPHER",
        "SINGLE_VIA_FLOW",
        "STRIPE_NODE_TYPE",
        "STRIPE_NODE_NETWORK",
        "STRIPE_NODE_FINGERPRINT",
        "STRIPE_NODE_TLS",
        "STRIPE_NODE_CIPHER",
        "STRIPE_NODE_FLOW",
        "FRONT_FINGERPRINT",
        "NODE_LABEL",
    }
)

# 按键名判定为敏感值的正则（覆盖 v1/v2 所有凭据、节点地址、路径密钥与用户自定义敏感字段）
SENSITIVE_KEY_RE = re.compile(
    r"(?:PASS|PASSWORD|PWD|SECRET|TOKEN|UUID|KEY|USER|USERNAME|SHORT_ID|"
    r"SERVER|SERVER_IP|HOST|WS_PATH|SNI|AUTH|CRED|COOKIE|BEARER|PRIVATE)",
    re.IGNORECASE,
)

# 通用文本脱敏正则（不依赖 instance.env 也能兜底清除日志/配置中的敏感信息）
PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
SSH_KEY_MATERIAL_RE = re.compile(
    r"\b(?:ssh-(?:ed25519|rsa|dss)|ecdsa-sha2-nistp\d+)\s+[A-Za-z0-9+/=]{16,}(?:\s+[^\s\r\n]+)?"
)
URL_USER_PASS_RE = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s:@'\"]+):([^/\s@'\"]+)@"
)
URL_USERINFO_RE = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s@'\"]+)@"
)
AUTH_HEADER_RE = re.compile(
    r"(?i)((?:Proxy-)?Authorization\s*:\s*(?:Basic|Bearer|Digest)\s+)[^\s\r\n'\"]+"
)
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
KV_SECRET_QUOTED_RE = re.compile(
    r"""(?im)(["']?(?:[a-z0-9_]*?(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|public[_-]?key|short[_-]?id|uuid|username|auth|credential)|[a-z0-9_]+_pass)["']?\s*[:=]\s*)(["'])[^"'\r\n]*\2"""
)
KV_SECRET_UNQUOTED_RE = re.compile(
    r"""(?im)(["']?(?:[a-z0-9_]*?(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|public[_-]?key|short[_-]?id|uuid|username|auth|credential)|[a-z0-9_]+_pass)["']?\s*[:=]\s*)(?!["'])([^\s,}\]\r\n]+)"""
)
HOME_PATH_MAC_RE = re.compile(r"(/Users/)([^/\s'\"\\]+)(?=/|\\|\s|'|$|\")")
HOME_PATH_LINUX_RE = re.compile(r"(/home/)([^/\s'\"\\]+)(?=/|\\|\s|'|$|\")")
HOME_PATH_WIN_BS_RE = re.compile(r"(?i)([A-Z]:\\+Users\\+)([^\\\s'\"]+)(?=\\|\s|'|$|\")")
HOME_PATH_WIN_FS_RE = re.compile(r"(?i)([A-Z]:/Users/)([^/\s'\"]+)(?=/|\s|'|$|\")")


def _get_host_username():
    try:
        user = getpass.getuser().strip()
        if len(user) >= 3:
            return user
    except Exception:
        pass
    return None


def _redact_host_home_and_user(text):
    """把本机家目录替换为 ~，把 macOS/Linux/Windows 路径中的用户名段兜底替换为 <user>，并清除当前宿主用户名。"""
    out = text
    try:
        home = Path.home()
        home_variants = []
        for variant in (str(home), home.as_posix(), str(home).replace("\\", "\\\\")):
            if variant and len(variant) >= 3 and variant not in home_variants:
                home_variants.append(variant)
        for variant in sorted(home_variants, key=len, reverse=True):
            if variant in out:
                out = out.replace(variant, "~")
    except Exception:
        pass

    out = HOME_PATH_MAC_RE.sub(r"\1<user>", out)
    out = HOME_PATH_LINUX_RE.sub(r"\1<user>", out)
    out = HOME_PATH_WIN_BS_RE.sub(r"\1<user>", out)
    out = HOME_PATH_WIN_FS_RE.sub(r"\1<user>", out)

    host_user = _get_host_username()
    if host_user and host_user in out:
        out = out.replace(host_user, "<user>")
    return out


def is_sensitive_env_key(key):
    """判断 instance.env 中的键是否应作为敏感值脱敏。"""
    upper = key.strip().upper()
    if upper in NON_SECRET_ENV_KEYS:
        return False
    # 明确排除端口与常见非敏感本地网络字段
    if upper.endswith("_PORT") or upper in (
        "ROUTER_IP",
        "HOME_LAN_CIDR",
        "VMNET_CIDR",
        "GUEST_IP",
        "EXPECTED_EGRESS_IP",
        "TART_BIN",
        "VM_STORAGE",
        "RUN_LOG",
        "PRIVATE_DIR",
        "SHARED_DIRS_FILE",
        "SSH_KNOWN_HOSTS",
        "VMX_PATH",
    ):
        return False
    return bool(SENSITIVE_KEY_RE.search(upper))


def extract_sensitive_values(env):
    """从解析后的 instance.env 字典提取所有需要精确脱敏的敏感字符串及其衍生编码。"""
    if not env:
        return []
    raw_secrets = set()
    for key, value in env.items():
        if not isinstance(value, str):
            continue
        val = value.strip()
        if not val:
            continue
        if is_sensitive_env_key(key):
            if len(val) >= 3:
                raw_secrets.add(val)
            # 若 SSH_PUBKEY 包含 "ssh-ed25519 AAAA... comment"，把关键 token 也加入精确表
            if key.upper() in ("SSH_PUBKEY", "SINGLE_HOST_KEY", "STRIPE_NODE_HOST_KEY"):
                for part in val.split():
                    if len(part) >= 12:
                        raw_secrets.add(part)

    # 补充常见配对组合（user:pass、URL 编码、HTTP Basic Auth base64）
    derived = set(raw_secrets)
    for prefix in ("WEBSHARE", "SINGLE", "STRIPE_NODE"):
        user = (env.get(f"{prefix}_USER") or "").strip()
        pwd = (env.get(f"{prefix}_PASS") or "").strip()
        if user and pwd:
            pair = f"{user}:{pwd}"
            derived.add(pair)
            b64 = base64.b64encode(pair.encode("utf-8")).decode("ascii")
            derived.add(b64)
    for s in list(raw_secrets):
        quoted = urllib.parse.quote(s, safe="")
        if quoted != s and len(quoted) >= 3:
            derived.add(quoted)

    host_user = _get_host_username()
    if host_user:
        derived.add(host_user)

    # 按长度降序排列，避免短子串抢先替换导致长串残留
    return sorted(derived, key=len, reverse=True)


def redact_text(text, secrets=()):
    """对任意文本执行统一脱敏：先做通用模式脱敏，再清除所有已知敏感值。"""
    if not text:
        return ""
    out = str(text)
    # 1. 通用结构化凭据模式
    out = PEM_PRIVATE_KEY_RE.sub("[REDACTED:PRIVATE_KEY]", out)
    out = SSH_KEY_MATERIAL_RE.sub("[REDACTED:SSH_KEY]", out)
    out = URL_USER_PASS_RE.sub(r"\1[REDACTED:USER]:[REDACTED:PASS]@", out)
    out = URL_USERINFO_RE.sub(r"\1[REDACTED:CRED]@", out)
    out = AUTH_HEADER_RE.sub(r"\1[REDACTED:AUTH]", out)
    out = UUID_RE.sub("[REDACTED:UUID]", out)
    out = KV_SECRET_QUOTED_RE.sub(r"\1\2[REDACTED]\2", out)
    out = KV_SECRET_UNQUOTED_RE.sub(r"\1[REDACTED]", out)

    # 2. 基于 instance.env 的精确值清除（兜底未被正则覆盖的任意自定义密码/域名/IP/路径）
    for secret in secrets:
        if secret and secret in out:
            out = out.replace(secret, "[REDACTED]")

    # 3. 宿主机家目录与本机用户名脱敏（避免对外发 Issue 暴露本机用户名）
    out = _redact_host_home_and_user(out)
    return out


def redact_obj(obj, secrets=()):
    """递归脱敏 JSON 兼容对象中的所有字符串值。"""
    if isinstance(obj, str):
        return redact_text(obj, secrets)
    if isinstance(obj, list):
        return [redact_obj(item, secrets) for item in obj]
    if isinstance(obj, dict):
        return {str(k): redact_obj(v, secrets) for k, v in obj.items()}
    return obj


def _run_cmd(cmd, timeout=15, env=None):
    """跨平台执行外部命令，返回 (returncode, stdout, stderr, error_reason)。"""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr, None
    except FileNotFoundError:
        return None, "", "", "command_not_found"
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return None, out, err, f"timeout_after_{timeout}s"
    except Exception as exc:
        return None, "", "", f"{type(exc).__name__}: {exc}"


def detect_tool_version(cmd, timeout=5):
    rc, stdout, stderr, err = _run_cmd(cmd, timeout=timeout)
    if err:
        return None
    combined = (stdout + "\n" + stderr).strip()
    if not combined:
        return None
    for line in combined.splitlines():
        s = line.strip()
        if s and ("version" in s.lower() or re.search(r"\d+\.\d+", s)):
            return s
    return combined.splitlines()[0].strip()


def collect_host_info():
    """收集宿主机系统版本、CPU 架构、Tart/VBoxManage/SSH 版本与仓库 commit。"""
    sys_name = platform.system()
    tart_bin = preflight_mod.find_tart()
    vboxmanage_bin = preflight_mod.find_vboxmanage()
    ssh_bin = shutil.which("ssh")

    tart_ver = detect_tool_version([tart_bin, "--version"]) if tart_bin else None
    vboxmanage_ver = detect_tool_version([vboxmanage_bin, "--version"]) if vboxmanage_bin else None
    ssh_ver = detect_tool_version([ssh_bin, "-V"]) if ssh_bin else None

    git_commit = None
    if shutil.which("git"):
        rc, out, _, err = _run_cmd(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], timeout=5)
        if err is None and rc == 0 and out.strip():
            git_commit = out.strip()

    info = {
        "os": sys_name,
        "os_release": platform.release(),
        "os_version": platform.version(),
        "platform": platform.platform(),
        "arch": preflight_mod.detect_arch(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "ram_gb": preflight_mod.detect_ram_gb(),
        "tart": {
            "found": bool(tart_bin),
            "version": tart_ver,
        },
        "vboxmanage": {
            "found": bool(vboxmanage_bin),
            "version": vboxmanage_ver,
        },
        "ssh_client": {
            "found": bool(ssh_bin),
            "version": ssh_ver,
        },
        "cchouse_git_commit": git_commit,
    }
    if sys_name == "Darwin":
        info["mac_ver"] = platform.mac_ver()[0]
    return info


def run_preflight(env_path=None, route="auto"):
    """调用 scripts/preflight.py 获取 JSON 自检结果。"""
    cmd = [sys.executable, "-I", str(ROOT / "scripts" / "preflight.py"), "--route", route]
    if env_path:
        cmd.extend(["--env", str(env_path)])
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    rc, stdout, stderr, err = _run_cmd(cmd, timeout=30, env=child_env)
    parsed = None
    if stdout.strip():
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None
    return {
        "exit_code": rc,
        "error": err,
        "json": parsed,
        "raw_stdout": stdout if parsed is None else None,
        "stderr": stderr.strip() or None,
    }


def _find_bash():
    if platform.system() == "Windows":
        for cand in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ):
            if os.path.isfile(cand):
                return cand
    return shutil.which("bash")


def run_ssh_command(env, remote_cmd, timeout=10):
    """通过 OpenSSH 在客机执行只读诊断命令。"""
    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        return None, "", "", "ssh_client_not_found"
    guest_ip = (env.get("GUEST_IP") or "").strip()
    ssh_key = (env.get("SSH_KEY") or "").strip()
    known_hosts = (env.get("SSH_KNOWN_HOSTS") or "").strip()
    if not guest_ip or not ssh_key:
        return None, "", "", "missing_guest_ip_or_ssh_key"
    if not os.path.isfile(ssh_key):
        return None, "", "", "ssh_key_file_not_found"

    cmd = [
        ssh_bin,
        "-F",
        "none",
        "-i",
        ssh_key,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout}",
    ]
    if known_hosts:
        cmd.extend(["-o", "StrictHostKeyChecking=yes", "-o", f'UserKnownHostsFile="{known_hosts}"'])
    else:
        cmd.extend(["-o", "StrictHostKeyChecking=yes"])
    cmd.extend([f"claude@{guest_ip}", remote_cmd])
    return _run_cmd(cmd, timeout=timeout + 10)


def run_verify_linux(env_path, timeout=60):
    """在宿主调用 scripts/verify-linux.sh <env> --json。"""
    bash_bin = _find_bash()
    if not bash_bin:
        return None, "", "", "bash_not_found"
    script_path = ROOT / "scripts" / "verify-linux.sh"
    if not script_path.is_file():
        return None, "", "", "verify_linux_script_not_found"
    cmd = [bash_bin, str(script_path), str(env_path), "--json"]
    return _run_cmd(cmd, timeout=timeout)


def build_env_summary(env):
    """仅保留白名单内的非敏感结构字段。"""
    if not env:
        return {}
    return {k: env[k] for k in SAFE_ENV_SUMMARY_KEYS if k in env and env[k] != ""}


def collect_guest_diagnostics(env, env_path, ssh_timeout=8):
    """尝试通过 SSH 收集客机日志、verify-linux --json、mihomo 状态与 nftables 规则。"""
    guest_files = {}
    missing = []
    expected_items = (
        "guest/firstboot.log",
        "guest/verify-linux.json",
        "guest/mihomo-status.txt",
        "guest/nft-ruleset.txt",
        "guest/image-release.json",
    )

    if not env:
        for item in expected_items:
            missing.append({"item": item, "reason": "no_instance_env_provided"})
        return False, guest_files, missing

    # 1. 先探测 SSH 连通性，不可达则快速失败并标注全部缺失项
    rc, _, stderr, err = run_ssh_command(env, "true", timeout=ssh_timeout)
    if err is not None or rc != 0:
        reason = err or f"ssh_unreachable(rc={rc}): {(stderr or '').strip()[:200]}"
        for item in expected_items:
            missing.append({"item": item, "reason": reason})
        return False, guest_files, missing

    guest_os = (env.get("GUEST_OS") or "macos").strip().lower()
    tag = (env.get("TAG") or "cchouse").strip()

    if guest_os == "linux":
        rc, out, _, err = run_ssh_command(env, "cat /etc/cchouse-image-release", timeout=ssh_timeout)
        identity = {}
        for line in out.splitlines() if err is None and rc == 0 else ():
            key, _, value = line.partition("=")
            patterns = {
                "SOURCE_COMMIT": r"[0-9a-f]{7,40}(?:-dirty)?",
                "BUILD_TIME": r"[0-9T:Z+-]{10,32}",
                "ARCH": r"amd64|arm64",
                "HYPERVISOR": r"tart|vmware|virtualbox",
            }
            if key in patterns and re.fullmatch(patterns[key], value):
                identity[key] = value
        if identity:
            guest_files["guest/image-release.json"] = json.dumps(identity, indent=2) + "\n"
        else:
            missing.append({"item": "guest/image-release.json", "reason": "guest_image_identity_unavailable"})
    else:
        missing.append({"item": "guest/image-release.json", "reason": f"not_applicable_for_{guest_os}"})

    # 2. firstboot.log
    if guest_os == "linux":
        rc, out, stderr, err = run_ssh_command(
            env,
            "if [ -f /var/lib/cchouse/firstboot.log ]; then cat /var/lib/cchouse/firstboot.log; else exit 44; fi",
            timeout=ssh_timeout,
        )
        if err is None and rc == 0:
            guest_files["guest/firstboot.log"] = out
        elif rc == 44:
            missing.append({"item": "guest/firstboot.log", "reason": "file_not_found_on_guest"})
        else:
            missing.append(
                {
                    "item": "guest/firstboot.log",
                    "reason": err or f"read_failed(rc={rc}): {(stderr or '').strip()[:200]}",
                }
            )
    else:
        missing.append({"item": "guest/firstboot.log", "reason": f"not_applicable_for_{guest_os}"})

    # 3. verify-linux.sh --json
    if guest_os == "linux" and env_path:
        rc, out, stderr, err = run_verify_linux(env_path, timeout=max(60, ssh_timeout * 5))
        if err is not None:
            missing.append({"item": "guest/verify-linux.json", "reason": err})
        else:
            payload = {
                "exit_code": rc,
                "result": None,
                "raw_stdout": None,
                "stderr": stderr.strip() or None,
            }
            if out.strip():
                try:
                    payload["result"] = json.loads(out.strip().splitlines()[-1])
                except json.JSONDecodeError:
                    payload["raw_stdout"] = out
            guest_files["guest/verify-linux.json"] = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    else:
        missing.append({"item": "guest/verify-linux.json", "reason": f"not_applicable_for_{guest_os}"})

    # 4. mihomo 运行状态
    if guest_os == "linux":
        mihomo_cmd = (
            "echo '=== is-active ==='; systemctl is-active cchouse-mihomo.service 2>&1 || true; "
            "echo '=== status ==='; systemctl status cchouse-mihomo.service --no-pager -n 30 2>&1 || true; "
            "echo '=== process ==='; ps -eo user=,pid=,comm= | grep mihomo || true; "
            "echo '=== journal ==='; journalctl -u cchouse-mihomo.service -n 80 --no-pager 2>&1 || true"
        )
    else:
        mihomo_cmd = (
            f"echo '=== launchctl ==='; launchctl print system/net.{tag}.mihomo 2>&1 || true; "
            "echo '=== process ==='; ps -eo user=,pid=,comm= | grep mihomo || true"
        )
    rc, out, stderr, err = run_ssh_command(env, mihomo_cmd, timeout=ssh_timeout)
    if err is None:
        guest_files["guest/mihomo-status.txt"] = out + (f"\n[stderr]\n{stderr}" if stderr.strip() else "")
    else:
        missing.append({"item": "guest/mihomo-status.txt", "reason": err})

    # 5. nft list ruleset（Linux 下 claude 仅允许受控探针，优先尝试 nft list ruleset 并附带 cchouse-probe 摘要）
    if guest_os == "linux":
        nft_cmd = (
            "echo '=== cchouse-probe nft-loaded ==='; "
            "sudo -n /usr/local/sbin/cchouse-probe nft-loaded 2>&1 || true; "
            "echo '=== cchouse-probe nft-live-sha ==='; "
            "sudo -n /usr/local/sbin/cchouse-probe nft-live-sha 2>&1 || true; "
            "echo '=== nft list ruleset ==='; "
            "if sudo -n /usr/sbin/nft list ruleset 2>&1; then "
            "  echo '__NFT_OK__'; "
            "elif /usr/sbin/nft list ruleset 2>&1; then "
            "  echo '__NFT_OK__'; "
            "else "
            "  echo '__NFT_DENIED__'; "
            "fi"
        )
        rc, out, stderr, err = run_ssh_command(env, nft_cmd, timeout=ssh_timeout)
        if err is None:
            combined = out + (f"\n[stderr]\n{stderr}" if stderr.strip() else "")
            if "__NFT_DENIED__" in combined and "__NFT_OK__" not in combined:
                missing.append(
                    {
                        "item": "guest/nft-ruleset.txt",
                        "reason": "nft_list_ruleset_requires_root_partial_probe_included",
                    }
                )
            guest_files["guest/nft-ruleset.txt"] = combined.replace("__NFT_OK__\n", "").replace("__NFT_DENIED__\n", "")
        else:
            missing.append({"item": "guest/nft-ruleset.txt", "reason": err})
    else:
        missing.append({"item": "guest/nft-ruleset.txt", "reason": f"not_applicable_for_{guest_os}"})

    return True, guest_files, missing


def _write_redacted_text(zf, arcname, text, secrets):
    clean = redact_text(text, secrets).replace("\r\n", "\n")
    zf.writestr(arcname, clean.encode("utf-8"))


def _write_redacted_json(zf, arcname, obj, secrets):
    clean_obj = redact_obj(obj, secrets)
    payload = json.dumps(clean_obj, ensure_ascii=False, indent=2) + "\n"
    # 再对序列化后的 JSON 文本做一次兜底脱敏，防止键名或转义形式漏网
    payload = redact_text(payload, secrets)
    zf.writestr(arcname, payload.encode("utf-8"))


def installed_image_from_state(state_path):
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        artifact = state.get("image_artifact")
        if (state.get("vm_created_by_up") is True and isinstance(artifact, dict)
                and isinstance(artifact.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])):
            origin = state.get("image_artifact_origin")
            return dict(image_identity(artifact), origin=origin)
    except (OSError, ValueError, AttributeError):
        pass
    return None


def build_doctor_bundle(env_path=None, vm_ssh=False, out_path=None, route="auto", ssh_timeout=8, state_path=None):
    """收集宿主机与可选客机诊断信息，统一脱敏并写入 zip 包，返回摘要字典。"""
    env = {}
    env_load_error = None
    if env_path:
        if os.path.isfile(env_path):
            try:
                env = render_mod.load_env(env_path)
            except SystemExit as exc:
                env_load_error = str(exc)
        else:
            env_load_error = f"env_file_not_found: {env_path}"

    secrets = extract_sensitive_values(env)
    host_info = collect_host_info()
    preflight_res = run_preflight(env_path=env_path, route=route)
    state_file = Path(state_path) if state_path else Path(env_path).with_name("state.json") if env_path else None
    installed_image = installed_image_from_state(state_file) if state_file else None

    missing = []
    if state_file and not installed_image:
        missing.append({"item": "installed_image", "reason": "state_has_no_imported_image_identity"})
    if env_load_error:
        missing.append({"item": "env-summary.json", "reason": env_load_error})

    vm_ssh_reachable = None
    guest_files = {}
    if vm_ssh:
        vm_ssh_reachable, guest_files, guest_missing = collect_guest_diagnostics(
            env=env, env_path=env_path, ssh_timeout=ssh_timeout
        )
        missing.extend(guest_missing)
    else:
        for item in (
            "guest/firstboot.log",
            "guest/verify-linux.json",
            "guest/mihomo-status.txt",
            "guest/nft-ruleset.txt",
            "guest/image-release.json",
        ):
            missing.append({"item": item, "reason": "vm_ssh_not_requested"})

    now = datetime.datetime.now(datetime.timezone.utc)
    if out_path is None:
        tag = re.sub(r"[^a-zA-Z0-9_-]+", "", (env.get("TAG") or "host")) or "host"
        stamp = now.strftime("%Y%m%d-%H%M%S")
        out_path = Path.cwd() / f"cchouse-doctor-{tag}-{stamp}.zip"
    else:
        out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    collected = ["manifest.json", "host-info.json", "preflight.json"]
    if env:
        collected.append("env-summary.json")
    collected.extend(sorted(guest_files.keys()))

    manifest = {
        "schema_version": 1,
        "cchouse_version": (ROOT / "VERSION").read_text(encoding="ascii").strip(),
        "installed_image": installed_image,
        "generated_at": now.isoformat(),
        "route_requested": route,
        "env_provided": bool(env_path),
        "vm_ssh_requested": bool(vm_ssh),
        "vm_ssh_reachable": vm_ssh_reachable,
        "preflight_exit_code": preflight_res.get("exit_code"),
        "collected": collected,
        "missing": missing,
        "redaction": {
            "env_secret_tokens_count": len(secrets),
            "uniform_text_redaction": True,
        },
    }

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_redacted_json(zf, "manifest.json", manifest, secrets)
        _write_redacted_json(zf, "host-info.json", host_info, secrets)
        _write_redacted_json(zf, "preflight.json", preflight_res, secrets)
        if env:
            _write_redacted_json(zf, "env-summary.json", build_env_summary(env), secrets)
        for arcname in sorted(guest_files.keys()):
            _write_redacted_text(zf, arcname, guest_files[arcname], secrets)

    if os.name != "nt":
        try:
            os.chmod(out_path, 0o600)
        except OSError:
            pass

    return {
        "ok": True,
        "zip_path": str(out_path),
        "vm_ssh_requested": bool(vm_ssh),
        "vm_ssh_reachable": vm_ssh_reachable,
        "collected": collected,
        "missing": redact_obj(missing, secrets),
    }


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="cchouse redacted diagnostic bundle exporter")
    parser.add_argument("--env", dest="env_path", default=None, help="instance.env 路径（仅用于自检、脱敏规则与可选 SSH 连接，绝不打包原文）")
    parser.add_argument("--state", dest="state_path", help="可选 state.json 路径，用于读取已导入镜像身份")
    parser.add_argument("--vm-ssh", action="store_true", help="尝试通过 SSH 拉取客机诊断日志（不可达时仍正常出包并标明缺失项）")
    parser.add_argument("--out", dest="out_path", default=None, help="输出 zip 文件路径（默认输出到当前目录）")
    parser.add_argument("--route", choices=("auto", "macos-tart", "tart-linux", "virtualbox-linux"), default="auto")
    parser.add_argument("--ssh-timeout", type=int, default=8, help="SSH 连接超时秒数（默认 8）")
    args = parser.parse_args()

    summary = build_doctor_bundle(
        env_path=args.env_path,
        vm_ssh=args.vm_ssh,
        out_path=args.out_path,
        route=args.route,
        ssh_timeout=args.ssh_timeout,
        state_path=args.state_path,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
