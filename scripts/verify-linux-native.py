#!/usr/bin/env python3
"""Run the Debian verifier via native OpenSSH, with no host Bash required."""

import argparse
import base64
import gzip
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import preflight  # noqa: E402
import render  # noqa: E402
import share as share_mod  # noqa: E402

VERIFY_TIMEOUT_SECONDS = 300

# Send only the verifier's non-secret inputs. Keys, proxy credentials, and host
# paths must never be passed to the guest.
GUEST_KEYS = (
    "TAG", "VM_NAME", "GUEST_IP", "EXPECTED_EGRESS_IP", "EXPECTED_EGRESS_ASN",
    "EXPECTED_EGRESS_LOC", "ROUTER_IP", "TIMEZONE", "GUEST_LOCALE",
    "MIHOMO_USER", "EXIT_MODE", "STRIPE_EXIT_MODE",
)


def _check_vbox_host_share(state, vm_name):
    if (state or {}).get("vm_backend") != "virtualbox" or not vm_name:
        return ""
    vbm = preflight.find_vboxmanage()
    if not vbm:
        return ""
    try:
        proc = subprocess.run(
            [vbm, "showvminfo", vm_name, "--machinereadable"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"fail:无法读取 VBoxManage showvminfo：{exc}"
    if proc.returncode != 0:
        return f"fail:VBoxManage showvminfo 退出码 {proc.returncode}"
    ok, detail = share_mod.check_vbox_showvminfo_share(proc.stdout.splitlines(), share_mod.normalize_share_state(state))
    return "ok" if ok else f"fail:{detail}"


def verify(env, flags, hostkey_trust="", share_cfg=None, vbox_host_share_check=""):
    guest_ip = str(ipaddress.IPv4Address(env["GUEST_IP"]))
    ipaddress.IPv4Address(env["ROUTER_IP"])
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,15}", env["TAG"]):
        raise ValueError("无效的 TAG")
    port = env.get("SSH_PORT") or "22"
    if not re.fullmatch(r"[1-9][0-9]{0,4}", port) or int(port) > 65535:
        raise ValueError("SSH_PORT 必须是 1-65535 的整数")

    ssh = "ssh"
    if os.name == "nt":
        ssh = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe")
    identity = Path(env["SSH_KEY"]).resolve()
    known_hosts = Path(env["SSH_KNOWN_HOSTS"]).resolve().as_posix()
    ssh_args = [
        ssh, "-T", "-F", "none", "-i", str(identity), "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f'UserKnownHostsFile="{known_hosts}"', "-p", port, f"claude@{guest_ip}",
    ]
    guest_env = dict(env)
    if env.get("EXIT_MODE") == "single" and env.get("STRIPE_NODE_SERVER") and not env.get("STRIPE_EXIT_MODE"):
        guest_env["STRIPE_EXIT_MODE"] = "node"
    config = "".join(f"export {key}={shlex.quote(guest_env.get(key, ''))}\n" for key in GUEST_KEYS)
    if hostkey_trust == "accept-new":
        config += "export CCHOUSE_HOSTKEY_TRUST=accept-new\n"
    norm_share = share_mod.normalize_share_state({"share": share_cfg}) if share_cfg else share_mod.normalize_share_state({})
    config += f"export CCHOUSE_SHARE_ENABLED={'1' if norm_share['enabled'] else '0'}\n"
    config += f"export CCHOUSE_SHARE_READ_ONLY={'1' if norm_share['read_only'] else '0'}\n"
    config += f"export CCHOUSE_SHARE_GUEST_PATH={shlex.quote(norm_share['guest_path'])}\n"
    if vbox_host_share_check:
        config += f"export CCHOUSE_VBOX_HOST_SHARE_CHECK={shlex.quote(vbox_host_share_check)}\n"
    shell_script = (SCRIPTS / "verify-linux.sh").read_text(encoding="utf-8")
    payload_bytes = (config + shell_script).encode("utf-8")
    command = "bash -s -- --guest-local " + " ".join(flags)
    if os.name == "nt":
        # Win32-OpenSSH hangs reading piped/file stdin when the parent has no console (e.g. ssh -T / AI agent).
        # Pass the non-secret verifier script via gzip+base64 with -n / DEVNULL instead.
        b64 = base64.b64encode(gzip.compress(payload_bytes)).decode("ascii")
        remote_cmd = f"printf %s {b64} | base64 -d | gzip -dc | {command}"
        win_args = [ssh_args[0], "-n", *ssh_args[1:], remote_cmd]
        return subprocess.run(
            win_args, stdin=subprocess.DEVNULL, timeout=VERIFY_TIMEOUT_SECONDS,
        ).returncode
    return subprocess.run(
        ssh_args + [command], input=payload_bytes, timeout=VERIFY_TIMEOUT_SECONDS,
    ).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env", type=Path)
    parser.add_argument("--fail-closed", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    flags = [flag for flag, enabled in (("--fail-closed", args.fail_closed), ("--json", args.json)) if enabled]
    try:
        trust_file = args.env.parent / "logs" / "hostkey.trust"
        trust = trust_file.read_text(encoding="utf-8-sig").strip() if trust_file.is_file() else ""
        state = {}
        state_file = args.env.parent / "state.json"
        if state_file.is_file():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
        env = render.load_env(args.env)
        share_cfg = share_mod.normalize_share_state(state)
        vbox_check = _check_vbox_host_share(state, state.get("vm_name") or env.get("VM_NAME") or "")
        return verify(env, flags, trust, share_cfg=share_cfg, vbox_host_share_check=vbox_check)
    except subprocess.TimeoutExpired:
        print(f"客机验收 SSH 超过 {VERIFY_TIMEOUT_SECONDS} 秒，已终止。", file=sys.stderr)
        return 124
    except (KeyError, OSError, ValueError) as error:
        print(f"无法启动客机验收：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
