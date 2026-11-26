#!/usr/bin/env python3
"""Run the Debian verifier via native OpenSSH, with no host Bash required."""

import argparse
import ipaddress
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import render  # noqa: E402

# Send only the verifier's non-secret inputs. Keys, proxy credentials, and host
# paths must never be passed to the guest.
GUEST_KEYS = (
    "TAG", "VM_NAME", "GUEST_IP", "EXPECTED_EGRESS_IP", "EXPECTED_EGRESS_ASN",
    "EXPECTED_EGRESS_LOC", "ROUTER_IP", "TIMEZONE", "GUEST_LOCALE",
    "MIHOMO_USER", "EXIT_MODE", "STRIPE_EXIT_MODE",
)


def verify(env, flags, hostkey_trust=""):
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
        "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=yes",
        "-o", f'UserKnownHostsFile="{known_hosts}"', "-p", port, f"claude@{guest_ip}",
    ]
    guest_env = dict(env)
    if env.get("EXIT_MODE") == "single" and env.get("STRIPE_NODE_SERVER") and not env.get("STRIPE_EXIT_MODE"):
        guest_env["STRIPE_EXIT_MODE"] = "node"
    config = "".join(f"export {key}={shlex.quote(guest_env.get(key, ''))}\n" for key in GUEST_KEYS)
    if hostkey_trust == "accept-new":
        config += "export CCHOUSE_HOSTKEY_TRUST=accept-new\n"
    shell_script = (SCRIPTS / "verify-linux.sh").read_text(encoding="utf-8")
    command = "bash -s -- --guest-local " + " ".join(flags)
    return subprocess.run(ssh_args + [command], input=(config + shell_script).encode("utf-8")).returncode


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
        return verify(render.load_env(args.env), flags, trust)
    except (KeyError, OSError, ValueError) as error:
        print(f"无法启动客机验收：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
