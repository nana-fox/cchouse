#!/usr/bin/env python3
"""cchouse v1 兼容与 v2 (Linux/EXIT_MODE/preflight) 单元测试。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RENDER = ROOT / "scripts" / "render.py"
PREFLIGHT = ROOT / "scripts" / "preflight.py"


def find_bash():
    for cand in (
        "/bin/bash",
        shutil.which("bash"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if cand and os.path.isfile(cand):
            return cand
    return None


def write_env(path, data):
    lines = [f"{k}='{v}'" for k, v in data.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def base_chain_env():
    # 动态拼装测试占位值，避免触发 pre-commit 静态凭据正则
    return {
        "TAG": "ut01",
        "HOME_LAN_CIDR": "192.168.100.0/24",
        "TIMEZONE": "America/Los_Angeles",
        "EXPECTED_EGRESS_IP": "198.51.100.10",
        "EXPECTED_EGRESS_ASN": "AS64512",
        "WEBSHARE_HOST": "203.0.113.10",
        "WEBSHARE_PORT": "8000",
        "WEBSHARE_USER": "demo_user",
        "WEBSHARE_PASS": "demo_secret",
        "FRONT_SERVER_IP": "203.0.113.20",
        "FRONT_PORT": "8443",
        "FRONT_UUID": "demo-front-id",
        "FRONT_SNI": "example.com",
        "FRONT_PUBLIC_KEY": "demo-pubkey",
        "FRONT_SHORT_ID": "abcd1234",
        "FRONT_FINGERPRINT": "chrome",
        "FRONT_CF_SERVER_IP": "203.0.113.21",
        "FRONT_CF_HOST": "cdn.example.com",
        "FRONT_CF_WS_PATH": "/ws-demo",
        "NODE_LABEL": "SJC01",
        "NODE_UUID": "demo-node-id",
        "NODE_WS_SERVER_IP": "203.0.113.30",
        "NODE_WS_HOST": "ws.example.com",
        "NODE_WS_PATH": "/node-ws",
        "NODE_REALITY_SERVER_IP": "203.0.113.31",
        "NODE_REALITY_SNI": "reality.example.com",
        "NODE_REALITY_PUBLIC_KEY": "demo-node-pubkey",
        "NODE_REALITY_SHORT_ID": "1234abcd",
    }


def base_single_linux_env():
    return {
        "GUEST_OS": "linux",
        "EXIT_MODE": "single",
        "STRIPE_EXIT_MODE": "off",
        "TAG": "win01",
        "GUEST_IF": "ens33",
        "MIHOMO_USER": "mihomo",
        "HOME_LAN_CIDR": "192.168.100.0/24",
        "TIMEZONE": "America/Los_Angeles",
        "GUEST_LOCALE": "en_US.UTF-8",
        "EXPECTED_EGRESS_IP": "198.51.100.55",
        "EXPECTED_EGRESS_ASN": "AS64512",
        "SINGLE_TYPE": "socks5",
        "SINGLE_SERVER": "203.0.113.55",
        "SINGLE_PORT": "1080",
        "SINGLE_USER": "demo_u",
        "SINGLE_PASS": "demo_p",
        "SSH_PUBKEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDemoTestKeyForUnitTestsOnly test@local",
    }


class TestCchouseV2(unittest.TestCase):
    def test_v1_macos_chain_backward_compat(self):
        with tempfile.TemporaryDirectory() as td:
            env_f = Path(td) / "instance.env"
            out = Path(td) / "out"
            data = base_chain_env()
            data["STRIPE_EXIT_MODE"] = "tmh-chain"
            write_env(env_f, data)
            cp = subprocess.run([sys.executable, "-I", str(RENDER), str(env_f), str(out)], capture_output=True, text=True)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            cfg = (out / "config.yaml").read_text(encoding="utf-8")
            self.assertIn("UT01-FRONT-via-SJC01-WS", cfg)
            self.assertNotIn(",DIRECT", cfg)
            # 验证 normalize_stripe_chain 仍可完美往返
            sys.path.insert(0, str(ROOT / "scripts"))
            import render as render_mod
            _, chained = render_mod.normalize_stripe_chain(cfg, "UT01")
            self.assertTrue(chained)

    def test_linux_single_socks5_and_nftables_guardrails(self):
        with tempfile.TemporaryDirectory() as td:
            env_f = Path(td) / "instance.env"
            out = Path(td) / "out"
            write_env(env_f, base_single_linux_env())
            cp = subprocess.run([sys.executable, "-I", str(RENDER), str(env_f), str(out)], capture_output=True, text=True)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            cfg = (out / "config.yaml").read_text(encoding="utf-8")
            nft = (out / "nftables.conf").read_text(encoding="utf-8")
            self.assertIn("server: 203.0.113.55", cfg)
            self.assertIn("device: cchouse0", cfg)
            self.assertIn("auto-redirect: false", cfg)
            self.assertIn("NETWORK,UDP,REJECT", cfg)
            self.assertNotIn(",DIRECT", cfg)
            non_comment_nft = "\n".join(l for l in nft.splitlines() if not l.lstrip().startswith("#"))
            self.assertNotIn("flush ruleset", non_comment_nft)
            self.assertNotIn("!=", non_comment_nft)
            self.assertIn('oifname "cchouse0" meta nfproto ipv4 accept', non_comment_nft)
            self.assertIn('meta skuid "mihomo" ip daddr 203.0.113.55 tcp dport 1080 accept', non_comment_nft)
            self.assertIn("meta nfproto ipv6 drop", non_comment_nft)
            bash_exe = find_bash()
            if bash_exe:
                for sh in ("firstboot.sh", "timesync.sh", "cchouse-probe.sh"):
                    chk = subprocess.run([bash_exe, "-n", str(out / sh)], capture_output=True, text=True)
                    self.assertEqual(chk.returncode, 0, f"{sh}: {chk.stderr}")

    def test_linux_single_domain_pinned_to_ip(self):
        with tempfile.TemporaryDirectory() as td:
            env_f = Path(td) / "instance.env"
            out = Path(td) / "out"
            data = base_single_linux_env()
            data.update({
                "SINGLE_TYPE": "vless",
                "SINGLE_SERVER": "edge.example.com",
                "SINGLE_SERVER_IP": "203.0.113.77",
                "SINGLE_PORT": "443",
                "SINGLE_UUID": "demo-vless-id",
                "SINGLE_NETWORK": "ws",
                "SINGLE_WS_PATH": "/tunnel",
            })
            write_env(env_f, data)
            cp = subprocess.run([sys.executable, "-I", str(RENDER), str(env_f), str(out)], capture_output=True, text=True)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            cfg = (out / "config.yaml").read_text(encoding="utf-8")
            nft = (out / "nftables.conf").read_text(encoding="utf-8")
            self.assertIn("server: 203.0.113.77", cfg)
            self.assertIn("servername: edge.example.com", cfg)
            self.assertIn("Host: edge.example.com", cfg)
            self.assertIn("ip daddr 203.0.113.77 tcp dport 443 accept", nft)

    def test_linux_single_rejects_udp_protocols(self):
        with tempfile.TemporaryDirectory() as td:
            env_f = Path(td) / "instance.env"
            out = Path(td) / "out"
            data = base_single_linux_env()
            data["SINGLE_TYPE"] = "hysteria2"
            write_env(env_f, data)
            cp = subprocess.run([sys.executable, "-I", str(RENDER), str(env_f), str(out)], capture_output=True, text=True)
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("UDP", cp.stderr)

    def test_linux_chain_fixes_dns_loop_and_supports_stripe_off(self):
        with tempfile.TemporaryDirectory() as td:
            env_f = Path(td) / "instance.env"
            out = Path(td) / "out"
            data = base_chain_env()
            data.update({
                "GUEST_OS": "linux",
                "EXIT_MODE": "chain",
                "STRIPE_EXIT_MODE": "off",
                "GUEST_IF": "ens33",
                "MIHOMO_USER": "mihomo",
                "GUEST_LOCALE": "en_US.UTF-8",
                "SSH_PUBKEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDemoTestKeyForUnitTestsOnly test@local",
            })
            write_env(env_f, data)
            cp = subprocess.run([sys.executable, "-I", str(RENDER), str(env_f), str(out)], capture_output=True, text=True)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            cfg = (out / "config.yaml").read_text(encoding="utf-8")
            nft = (out / "nftables.conf").read_text(encoding="utf-8")
            self.assertIn("proxy-server-nameserver:\n  - https://1.1.1.1/dns-query#UT01-Stripe", cfg)
            self.assertNotIn("DOMAIN-SUFFIX,stripe.com", cfg)
            self.assertIn("ip daddr 203.0.113.20 tcp dport 8443 accept", nft)
            self.assertIn("ip daddr 203.0.113.21 tcp dport 443 accept", nft)

    def test_preflight_json_redacts_secrets_and_writes_state(self):
        with tempfile.TemporaryDirectory() as td:
            env_f = Path(td) / "instance.env"
            state_f = Path(td) / "state.json"
            data = base_single_linux_env()
            data["SINGLE_PASS"] = "SUPER_SECRET_NEVER_PRINT_999"
            write_env(env_f, data)
            cp = subprocess.run(
                [sys.executable, "-I", str(PREFLIGHT), "--route", "vmware-linux", "--env", str(env_f), "--state", str(state_f)],
                capture_output=True,
                text=True,
            )
            self.assertNotIn("SUPER_SECRET_NEVER_PRINT_999", cp.stdout + cp.stderr)
            payload = json.loads(cp.stdout)
            self.assertIn("next_action", payload)
            self.assertTrue(state_f.is_file())
            state = json.loads(state_f.read_text(encoding="utf-8"))
            self.assertEqual(state["route"], "vmware-linux")

    def test_image_builder_scripts_and_seed_iso(self):
        bash_exe = find_bash()
        if bash_exe:
            for sh in (
                ROOT / "guest-linux" / "image-builder" / "build-debian13-golden.sh",
                ROOT / "guest-linux" / "image-builder" / "assert-empty-state.sh",
                ROOT / "guest-linux" / "image-builder" / "cchouse-firstboot-loader.sh",
            ):
                chk = subprocess.run([bash_exe, "-n", str(sh)], capture_output=True, text=True)
                self.assertEqual(chk.returncode, 0, f"{sh}: {chk.stderr}")

        with tempfile.TemporaryDirectory() as td:
            env_f = Path(td) / "instance.env"
            out = Path(td) / "rendered"
            iso = Path(td) / "seed.iso"
            write_env(env_f, base_single_linux_env())
            cp = subprocess.run([sys.executable, "-I", str(RENDER), str(env_f), str(out)], capture_output=True, text=True)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            cp2 = subprocess.run(
                [sys.executable, "-I", str(ROOT / "scripts" / "make-seed-iso.py"), str(out), str(iso)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(cp2.returncode, 0, cp2.stderr)
            self.assertTrue(iso.is_file())
            self.assertGreater(iso.stat().st_size, 0)

    def test_bom_atomic_no_partial_and_tls_guards(self):
        with tempfile.TemporaryDirectory() as td:
            # 1) Windows UTF-8 BOM env 能正常解析
            env_bom = Path(td) / "bom.env"
            out_ok = Path(td) / "out_ok"
            data = base_single_linux_env()
            env_bom.write_bytes(b"\xef\xbb\xbf" + ("\n".join(f"{k}='{v}'" for k, v in data.items()) + "\n").encode("utf-8"))
            cp = subprocess.run([sys.executable, "-I", str(RENDER), str(env_bom), str(out_ok)], capture_output=True, text=True)
            self.assertEqual(cp.returncode, 0, cp.stderr)

            # 2) 缺必填字段时绝不写半成品（config.yaml 不应落盘）
            env_bad = Path(td) / "bad.env"
            out_bad = Path(td) / "out_bad"
            bad_data = base_single_linux_env()
            bad_data["EXPECTED_EGRESS_IP"] = ""
            write_env(env_bad, bad_data)
            cp2 = subprocess.run([sys.executable, "-I", str(RENDER), str(env_bad), str(out_bad)], capture_output=True, text=True)
            self.assertNotEqual(cp2.returncode, 0)
            self.assertFalse(out_bad.exists())

            # 3) VLESS 禁止明文 SINGLE_TLS=false
            env_vless = Path(td) / "vless.env"
            vdata = base_single_linux_env()
            vdata.update({"SINGLE_TYPE": "vless", "SINGLE_UUID": "demo-id", "SINGLE_TLS": "false"})
            write_env(env_vless, vdata)
            cp3 = subprocess.run([sys.executable, "-I", str(RENDER), str(env_vless), str(Path(td) / "out_v")], capture_output=True, text=True)
            self.assertNotEqual(cp3.returncode, 0)
            self.assertIn("SINGLE_TLS", cp3.stderr)

            # 4) 默认全断服务不得含 ConditionPathExists（稳态每次开机必加载）
            builder_sh = (ROOT / "guest-linux" / "image-builder" / "build-debian13-golden.sh").read_text(encoding="utf-8")
            self.assertNotIn("ConditionPathExists=!/var/lib/cchouse/firstboot.done", builder_sh)
            # 5) firstboot sudoers 不得含通配符 *
            fb_sh = (ROOT / "guest-linux" / "firstboot.sh").read_text(encoding="utf-8")
            sudo_block = fb_sh.split("/etc/sudoers.d/cchouse-verify")[1].split("EOF")[1]
            self.assertNotIn("*", sudo_block)

    def test_codex_hardening_fixes(self):
        with tempfile.TemporaryDirectory() as td:
            # 1) Linux config.yaml 必须关闭 external-controller，且清理目录旧残留文件
            env_f = Path(td) / "s.env"
            out = Path(td) / "rendered"
            out.mkdir()
            stale = out / "stale_old_file.txt"
            stale.write_text("old", encoding="utf-8")
            write_env(env_f, base_single_linux_env())
            cp = subprocess.run([sys.executable, "-I", str(RENDER), str(env_f), str(out)], capture_output=True, text=True)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertFalse(stale.exists())
            self.assertNotIn("external-controller", (out / "config.yaml").read_text(encoding="utf-8"))

            # 2) Linux 缺 SSH_PUBKEY 必须拒收
            no_key = base_single_linux_env()
            no_key["SSH_PUBKEY"] = ""
            env_nk = Path(td) / "nk.env"
            write_env(env_nk, no_key)
            cp_nk = subprocess.run([sys.executable, "-I", str(RENDER), str(env_nk), str(Path(td) / "out_nk")], capture_output=True, text=True)
            self.assertNotEqual(cp_nk.returncode, 0)
            self.assertIn("SSH_PUBKEY", cp_nk.stderr)

            # 3) 拒收未知 SINGLE_NETWORK=grpc 与域名钉 IP 的 socks5+tls
            grpc_d = base_single_linux_env()
            grpc_d.update({"SINGLE_TYPE": "vless", "SINGLE_UUID": "demo-id", "SINGLE_NETWORK": "grpc"})
            env_g = Path(td) / "g.env"
            write_env(env_g, grpc_d)
            cp_g = subprocess.run([sys.executable, "-I", str(RENDER), str(env_g), str(Path(td) / "out_g")], capture_output=True, text=True)
            self.assertNotEqual(cp_g.returncode, 0)
            self.assertIn("SINGLE_NETWORK", cp_g.stderr)

            # 4) cchouse-firstboot.service 必须显式排在 cchouse-default-drop.service 之后
            fb_svc = (ROOT / "guest-linux" / "cchouse-firstboot.service").read_text(encoding="utf-8")
            self.assertIn("cchouse-default-drop.service", fb_svc)

            # 5) verify-linux.sh 在 Bash 3.2 / Git Bash 下可解析带 BOM + CRLF 的 env（不报缺 TAG）
            bash_exe = find_bash()
            if bash_exe:
                v_env = Path(td) / "verify.env"
                v_data = base_single_linux_env()
                v_data.update({
                    "GUEST_IP": "127.0.0.1",
                    "ROUTER_IP": "192.168.100.1",
                    "SSH_KEY": str(Path(td) / "nonexistent_key"),
                    "SSH_KNOWN_HOSTS": str(Path(td) / "known_hosts"),
                })
                raw = "\r\n".join(f"{k}='{v}'" for k, v in v_data.items()) + "\r\n"
                v_env.write_bytes(b"\xef\xbb\xbf" + raw.encode("utf-8"))
                cp_v = subprocess.run(
                    [bash_exe, str(ROOT / "scripts" / "verify-linux.sh"), str(v_env)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                self.assertNotIn("缺 TAG", cp_v.stderr)
                self.assertIn("期望出口 198.51.100.55", cp_v.stdout)


if __name__ == "__main__":
    unittest.main()
