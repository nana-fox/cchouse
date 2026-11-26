#!/usr/bin/env python3
"""cchouse v1 兼容与 v2 (Linux/EXIT_MODE/preflight) 单元测试。"""
import contextlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
            self.assertIn('iifname "cchouse0" meta nfproto ipv4 accept', non_comment_nft)
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
                [sys.executable, "-I", str(PREFLIGHT), "--route", "virtualbox-linux", "--env", str(env_f), "--state", str(state_f)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertNotIn("SUPER_SECRET_NEVER_PRINT_999", cp.stdout + cp.stderr)
            payload = json.loads(cp.stdout)
            self.assertIn("next_action", payload)
            self.assertTrue(state_f.is_file())
            state = json.loads(state_f.read_text(encoding="utf-8"))
            self.assertEqual(state["route"], "virtualbox-linux")
            self.assertIn("ram_gb", payload["host"])
            # 验证 --set-step / --set-status 可推进 state.json
            cp2 = subprocess.run(
                [sys.executable, "-I", str(PREFLIGHT), "--route", "virtualbox-linux", "--state", str(state_f),
                 "--set-step", "step1_route", "--set-status", "HUMAN_WAIT", "--next-action", "wait_user_proxy"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            state2 = json.loads(state_f.read_text(encoding="utf-8"))
            self.assertEqual(state2["step"], "step1_route")
            self.assertEqual(state2["status"], "HUMAN_WAIT")
            self.assertEqual(state2["next_action"], "wait_user_proxy")

    def test_install_disk_thresholds_match_measured_footprint(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import preflight as preflight_mod

        # Mac Tart 只读实测：缓存 + 黄金镜像 + 客机 + 干净快照。最低值不得低于这次占用。
        measured = 1109405696 + 2268217344 + 2781511680 + 2674987008
        self.assertEqual(measured, 8834121728)
        self.assertGreaterEqual(preflight_mod.TART_LINUX_DISK_MIN_GB, measured / preflight_mod.BYTES_PER_GB)
        self.assertEqual(preflight_mod.TART_LINUX_DISK_MIN_GB, 9)
        self.assertEqual(preflight_mod.TART_LINUX_DISK_RECOMMENDED_GB, 12)
        self.assertGreater(
            preflight_mod.TART_LINUX_DISK_RECOMMENDED_GB,
            preflight_mod.TART_LINUX_DISK_MIN_GB,
        )

        # Windows 只按 VirtualBox 实测。与 Mac 一样按十进制 GB 向上取整，再留约 3 GB。
        vbox_kept = 1770708992 + 4154236928
        vbox_drop = 6196707328
        self.assertEqual(vbox_kept, 5924945920)
        self.assertEqual(math.ceil(vbox_drop / 1_000_000_000), 7)
        self.assertEqual(preflight_mod.VIRTUALBOX_DISK_MIN_GB, 7)
        self.assertEqual(preflight_mod.VIRTUALBOX_DISK_RECOMMENDED_GB, 10)
        self.assertEqual(preflight_mod.disk_requirement_gb("virtualbox-linux"), (7, 10, False))

        below, _warn = preflight_mod.assess_install_disk(8.9, "tart-linux")
        self.assertTrue(any("INSUFFICIENT_DISK" in item and "至少需要 9 GB" in item for item in below))
        self.assertEqual(_warn, [])
        between_err, between_warn = preflight_mod.assess_install_disk(9.0, "tart-linux")
        self.assertEqual(between_err, [])
        self.assertTrue(any("LOW_DISK" in item and "建议至少 12 GB" in item for item in between_warn))
        self.assertFalse(any("估算" in item for item in between_warn))
        ok_err, ok_warn = preflight_mod.assess_install_disk(12.0, "tart-linux")
        self.assertEqual((ok_err, ok_warn), ([], []))

        win_below, win_warn = preflight_mod.assess_install_disk(7 - 1 / preflight_mod.BYTES_PER_GB, "virtualbox-linux")
        self.assertTrue(any("INSUFFICIENT_DISK" in item and "至少需要 7 GB" in item for item in win_below))
        self.assertFalse(any("估算" in item for item in win_below))
        self.assertEqual(win_warn, [])
        win_mid_err, win_mid_warn = preflight_mod.assess_install_disk(7.0, "virtualbox-linux")
        self.assertEqual(win_mid_err, [])
        self.assertTrue(any("建议至少 10 GB" in item for item in win_mid_warn))
        self.assertFalse(any("估算" in item for item in win_mid_warn))
        win_ok_err, win_ok_warn = preflight_mod.assess_install_disk(10.0, "virtualbox-linux")
        self.assertEqual((win_ok_err, win_ok_warn), ([], []))

        # v1 macOS 客机仍走 30/50 GB，不套用 Debian 实测值。
        self.assertEqual(preflight_mod.assess_install_disk(1.0, "macos-tart"), ([], []))

        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "not-created" / "cchouse"
            found = preflight_mod.existing_volume_path(missing)
            self.assertEqual(found, Path(td))
            self.assertFalse(missing.exists())
            self.assertFalse((Path(td) / "not-created").exists())

        ps1 = (ROOT / "scripts" / "preflight.ps1").read_text(encoding="utf-8")
        self.assertIn("$diskMinGb = 7", ps1)
        self.assertIn("$diskRecommendedGb = 10", ps1)
        self.assertIn("/ 1000000000.0", ps1)
        self.assertIn("至少需要 $diskMinGb GB", ps1)
        self.assertIn("建议至少 $diskRecommendedGb GB", ps1)
        self.assertIn("disk_estimate = $false", ps1)
        self.assertNotIn("估算", ps1)

    def test_virtualbox_preflight_reads_install_volume_and_reports_disk_failure(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import preflight as preflight_mod

        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            target = parent / "missing" / "cchouse"
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["preflight.py", "--route", "virtualbox-linux"]), \
                    mock.patch.object(preflight_mod, "default_install_dir", return_value=target), \
                    mock.patch.object(preflight_mod.shutil, "disk_usage", return_value=mock.Mock(free=6_999_999_999)) as usage, \
                    contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_info:
                preflight_mod.main()
            self.assertEqual(exit_info.exception.code, 1)
            usage.assert_called_once_with(parent)
            self.assertFalse(target.exists())
            result = json.loads(output.getvalue())
            self.assertEqual(result["next_action"], "free_disk")
            self.assertEqual(result["host"]["disk_min_gb"], 7)
            self.assertEqual(result["host"]["disk_recommended_gb"], 10)
            self.assertTrue(any(error.startswith("INSUFFICIENT_DISK:") for error in result["errors"]))

    def test_tart_cached_image_ignores_deployed_vms(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import preflight as preflight_mod
        with tempfile.TemporaryDirectory() as td:
            th = Path(td)
            tag_dir = th / "cache" / "OCIs" / "ghcr.io" / "cirruslabs" / "macos-tahoe-vanilla"
            tag_dir.mkdir(parents=True)
            # 模拟 tart prune 残留的空壳目录与悬空 tag 软链（无实际 disk.img）
            if os.name != "nt":
                (tag_dir / "26.6.2").symlink_to(tag_dir / "sha256:missing")
            (th / "vms" / "claude-us03").mkdir(parents=True)
            (th / "vms" / "claude-us03" / "disk.img").write_bytes(b"x")
            (th / "vms" / "claude-us03-clean-20261115").mkdir(parents=True)
            (th / "vms" / "claude-us03-clean-20261115" / "disk.img").write_bytes(b"x")
            self.assertFalse(preflight_mod.has_tart_cached_image(th))

            base_vm = th / "vms" / "macos-tahoe-vanilla-26.6.2"
            base_vm.mkdir(parents=True)
            (base_vm / "disk.img").write_bytes(b"x")
            self.assertTrue(preflight_mod.has_tart_cached_image(th))

        with tempfile.TemporaryDirectory() as td:
            th = Path(td)
            digest_dir = "sha256-abc" if os.name == "nt" else "sha256:abc"
            img_dir = th / "cache" / "OCIs" / "ghcr.io" / "cirruslabs" / "macos-tahoe-vanilla" / digest_dir
            img_dir.mkdir(parents=True)
            (img_dir / "disk.img").write_bytes(b"x")
            self.assertTrue(preflight_mod.has_tart_cached_image(th))

    def test_tart_linux_builder_tarxz_and_golden_detect(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import preflight as preflight_mod
        builder = (ROOT / "guest-linux" / "image-builder" / "build-tart-linux-from-cloud-image.sh").read_text(encoding="utf-8")
        self.assertIn(".tar.xz", builder)
        self.assertIn("CLOUD_IMAGE_VERSION", builder)
        self.assertIn('dirname "$DISK_IMG"', builder)
        self.assertIn("${CLOUD_IMG}.part", builder)
        self.assertIn("不支持的云镜像文件格式", builder)
        self.assertNotIn("/trixie/latest/debian-13-generic-arm64.raw", builder)
        bash_exe = find_bash()
        if bash_exe:
            chk = subprocess.run(
                [bash_exe, "-n", str(ROOT / "guest-linux" / "image-builder" / "build-tart-linux-from-cloud-image.sh")],
                capture_output=True,
                text=True,
            )
            self.assertEqual(chk.returncode, 0, chk.stderr)
        with tempfile.TemporaryDirectory() as td:
            th = Path(td) / "ext-tart-home"
            (th / "vms" / "claude-deb-qa01").mkdir(parents=True)
            (th / "vms" / "claude-deb-qa01" / "disk.img").write_bytes(b"x")
            (th / "vms" / "cchouse-debian13-arm64-empty").mkdir(parents=True)
            (th / "vms" / "cchouse-debian13-arm64-test").mkdir(parents=True)
            (th / "vms" / "cchouse-debian13-arm64-test" / "disk.img").write_bytes(b"x")
            self.assertEqual(preflight_mod.find_tart_linux_golden_images(th), [])
            # 临时构建机（-building 后缀）不得被识别为黄金镜像
            (th / "vms" / "cchouse-debian13-arm64-building").mkdir(parents=True)
            (th / "vms" / "cchouse-debian13-arm64-building" / "disk.img").write_bytes(b"x")
            (th / "vms" / "cchouse-debian13-arm64-v2.0.0-preview.2-building").mkdir(parents=True)
            (th / "vms" / "cchouse-debian13-arm64-v2.0.0-preview.2-building" / "disk.img").write_bytes(b"x")
            self.assertEqual(preflight_mod.find_tart_linux_golden_images(th), [])
            # 正式命名（含 tart import 导入的黄金镜像，无需额外标记文件）应被正常识别
            (th / "vms" / "cchouse-debian13-arm64").mkdir(parents=True)
            (th / "vms" / "cchouse-debian13-arm64" / "disk.img").write_bytes(b"x")
            (th / "vms" / "cchouse-debian13-arm64-v2.0.0-preview.2").mkdir(parents=True)
            (th / "vms" / "cchouse-debian13-arm64-v2.0.0-preview.2" / "disk.img").write_bytes(b"x")
            self.assertEqual(
                preflight_mod.find_tart_linux_golden_images(th),
                ["cchouse-debian13-arm64", "cchouse-debian13-arm64-v2.0.0-preview.2"],
            )
            # 验证 host/tart-wrapper.sh.example 形式的外置盘包装脚本可正确解析 TART_HOME
            wrapper = Path(td) / "tart"
            wrapper.write_text(f'#!/bin/sh\nexport TART_HOME="{th}"\nexec /bin/true "$@"\n', encoding="utf-8")
            old_env = os.environ.pop("TART_HOME", None)
            try:
                resolved = preflight_mod.tart_home_dir(str(wrapper))
                self.assertEqual(resolved, th)
                self.assertEqual(
                    preflight_mod.find_tart_linux_golden_images(resolved),
                    ["cchouse-debian13-arm64", "cchouse-debian13-arm64-v2.0.0-preview.2"],
                )
            finally:
                if old_env is not None:
                    os.environ["TART_HOME"] = old_env

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

            # 4) 默认全断服务不得含 ConditionPathExists（稳态每次开机必加载），且具备完整中文桌面与 Tart 剪贴板/DHCP 支持
            builder_sh = (ROOT / "guest-linux" / "image-builder" / "build-debian13-golden.sh").read_text(encoding="utf-8")
            self.assertNotIn("ConditionPathExists=!/var/lib/cchouse/firstboot.done", builder_sh)
            for marker in ("fonts-noto-cjk", "fcitx5-chinese-addons", "dbus-user-session", "vmware-user-suid-wrapper", "tint2", "lxterminal", "spice-vdagent", "xclip", "xset s off -dpms", "ClientIdentifier=mac"):
                self.assertIn(marker, builder_sh)
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

            # 5) verify-linux.sh 必须包含 -F none 与带内层引号的 UserKnownHostsFile（兼容含空格路径如 Application Support）
            vl_sh = (ROOT / "scripts" / "verify-linux.sh").read_text(encoding="utf-8")
            self.assertIn("-F none", vl_sh)
            self.assertIn('UserKnownHostsFile=\\"$SSH_KNOWN_HOSTS\\"', vl_sh)

            # 6) verify-linux.sh 在 Bash 3.2 / Git Bash 下可解析带 BOM + CRLF 的 env（不报缺 TAG）
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

    def test_ssh_nets_collapse_and_audit_fixes(self):
        with tempfile.TemporaryDirectory() as td:
            # 重复 + 重叠 + 带主机位 CIDR 均应自动折叠规范化
            env = base_single_linux_env()
            env["VMNET_CIDR"] = "192.168.64.5/24"
            env["HOME_LAN_CIDR"] = "192.168.0.0/16"
            env_p = Path(td) / "overlap.env"
            out_d = Path(td) / "out_overlap"
            write_env(env_p, env)
            cp = subprocess.run(
                [sys.executable, "-I", str(ROOT / "scripts" / "render.py"), str(env_p), str(out_d)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(cp.returncode, 0, cp.stderr)
            nft = (out_d / "nftables.conf").read_text(encoding="utf-8")
            self.assertIn("elements = { 192.168.0.0/16 }", nft)

            # firstboot 写入 managed-settings.json，assert-empty-state 校验持久化 DHCP
            fb = (ROOT / "guest-linux" / "firstboot.sh").read_text(encoding="utf-8")
            self.assertIn("/etc/claude-code/managed-settings.json", fb)
            aes = (ROOT / "guest-linux" / "image-builder" / "assert-empty-state.sh").read_text(encoding="utf-8")
            self.assertIn("20-cchouse-dhcp.network", aes)
            self.assertIn("systemd-networkd.service", aes)

    def test_tun_route_exclude_covers_vmnet_in_both_modes(self):
        # 客机 NAT 网段与家庭局域网分别绕过 TUN，避免宿主 SSH 回包被劫持
        import re as _re
        cases = [("single", base_single_linux_env()), ("chain", base_chain_env())]
        for mode, env in cases:
            env = dict(env)
            if mode == "chain":
                env.update({"GUEST_OS": "linux", "EXIT_MODE": "chain", "STRIPE_EXIT_MODE": "front"})
                env.setdefault("SSH_PUBKEY", base_single_linux_env()["SSH_PUBKEY"])
            env["VMNET_CIDR"] = "192.168.140.0/24"
            env["HOME_LAN_CIDR"] = "192.168.124.0/24"
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                env_p = Path(td) / "instance.env"
                out_d = Path(td) / "out"
                write_env(env_p, env)
                cp = subprocess.run(
                    [sys.executable, "-I", str(RENDER), str(env_p), str(out_d)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                self.assertEqual(cp.returncode, 0, cp.stderr)
                cfg = (out_d / "config.yaml").read_text(encoding="utf-8")
                m = _re.search(r"  route-exclude-address:\n((?:  - [^\n]*\n)+)", cfg)
                self.assertIsNotNone(m, cfg)
                items = [l[4:] for l in m.group(1).splitlines()]
                self.assertEqual(items, ["192.168.64.0/24", "192.168.124.0/24", "192.168.140.0/24"])
                self.assertEqual(cfg.count("route-exclude-address:"), 1)

    def test_linux_desktop_ux_and_review_hardening(self):
        import configparser

        # 1) chromium-policy.json 可正常 JSON 解析且关闭书签栏
        policy = json.loads((ROOT / "guest-linux" / "chromium-policy.json").read_text(encoding="utf-8"))
        self.assertIs(policy.get("BookmarkBarEnabled"), False)

        # 2) build-debian13-golden.sh 满足所有桌面、输入法、安全与 DHCPv4 约束
        builder_sh = (ROOT / "guest-linux" / "image-builder" / "build-debian13-golden.sh").read_text(encoding="utf-8")
        self.assertNotIn("--disable-session-crashed-bubble", builder_sh)
        self.assertNotIn("fcitx5-module-xorg", builder_sh)
        self.assertNotIn("panel_items = TSBC", builder_sh)
        self.assertNotIn("Name=keyboard-us", builder_sh)
        self.assertNotIn("Default Layout=us", builder_sh)
        for marker in (
            "dbus-user-session",
            "/etc/chromium.d/cchouse",
            "${CHROMIUM_FLAGS:-}",
            "--hide-crash-restore-bubble",
            "CloudPinyinEnabled=False",
            "panel_items = TSC",
            "1=Alt+space",
            'key="A-space"',
            "vmware-user-suid-wrapper",
            "/etc/issue.d/cchouse.issue",
            "(sleep 1 && chromium) &",
            "(sleep 1 && lxterminal) &",
            "/home/claude/.local/share/fcitx5",
            "sudo -n /usr/bin/systemctl poweroff",
            "sudo -n /usr/bin/systemctl reboot",
        ):
            self.assertIn(marker, builder_sh)

        # cchouse-power sudoers 不得含通配符 *
        power_block = builder_sh.split("/etc/sudoers.d/cchouse-power")[1].split("EOF")[1]
        self.assertNotIn("*", power_block)
        self.assertIn("/usr/bin/systemctl poweroff", power_block)
        self.assertIn("/usr/bin/systemctl reboot", power_block)

        # 20-cchouse-dhcp.network 的 [DHCPv4] 段内精确校验
        dhcp_block = builder_sh.split("20-cchouse-dhcp.network <<'EOF'\n", 1)[1].split("\nEOF", 1)[0]
        cp_ini = configparser.ConfigParser()
        cp_ini.read_string(dhcp_block)
        self.assertTrue(cp_ini.has_section("DHCPv4"))
        self.assertEqual(cp_ini.get("DHCPv4", "ClientIdentifier"), "mac")
        self.assertEqual(cp_ini.get("DHCPv4", "UseDNS"), "no")
        self.assertEqual(cp_ini.get("DHCPv4", "UseNTP"), "no")

        # 3) assert-empty-state.sh 校验无 fcitx5 词库残留与无硬编码 profile
        aes = (ROOT / "guest-linux" / "image-builder" / "assert-empty-state.sh").read_text(encoding="utf-8")
        self.assertIn("/home/claude/.local/share/fcitx5", aes)
        self.assertIn("/home/claude/.config/fcitx5/profile", aes)

        # 4) 非 us 的 GUEST_KEYBOARD 渲染到 firstboot.sh 后动态生成对应 fcitx5 profile
        with tempfile.TemporaryDirectory() as td:
            env = base_single_linux_env()
            env["GUEST_KEYBOARD"] = "gb"
            env_p = Path(td) / "gb.env"
            out_d = Path(td) / "out_gb"
            write_env(env_p, env)
            cp = subprocess.run(
                [sys.executable, "-I", str(RENDER), str(env_p), str(out_d)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(cp.returncode, 0, cp.stderr)
            fb_rendered = (out_d / "firstboot.sh").read_text(encoding="utf-8")
            self.assertIn('GUEST_KEYBOARD="gb"', fb_rendered)
            self.assertIn("Default Layout=${GUEST_KEYBOARD}", fb_rendered)
            self.assertIn("DefaultIM=pinyin", fb_rendered)
            self.assertIn("Name=keyboard-${GUEST_KEYBOARD}", fb_rendered)
            bash_exe = find_bash()
            if bash_exe:
                chk = subprocess.run([bash_exe, "-n", str(out_d / "firstboot.sh")], capture_output=True, text=True)
                self.assertEqual(chk.returncode, 0, chk.stderr)

            # 5) 非法 GUEST_KEYBOARD（多布局/变体）被 render.py 与 preflight.py 拒收
            for bad_kb in ("us,de", "us(intl)", "US"):
                bad_env = base_single_linux_env()
                bad_env["GUEST_KEYBOARD"] = bad_kb
                bad_p = Path(td) / f"bad_kb_{bad_kb.replace(',', '_').replace('(', '_').replace(')', '_')}.env"
                write_env(bad_p, bad_env)
                cp_r = subprocess.run([sys.executable, "-I", str(RENDER), str(bad_p), str(Path(td) / "out_bad_kb")], capture_output=True, text=True)
                self.assertNotEqual(cp_r.returncode, 0)
                self.assertIn("INVALID_GUEST_KEYBOARD", cp_r.stderr)
                cp_p = subprocess.run(
                    [sys.executable, "-I", str(PREFLIGHT), "--route", "tart-linux", "--env", str(bad_p)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                self.assertNotEqual(cp_p.returncode, 0)
                self.assertIn("INVALID_GUEST_KEYBOARD", cp_p.stdout)

        # 6) verify-linux.sh 含 sudo -n -l 白名单校验
        vl_sh = (ROOT / "scripts" / "verify-linux.sh").read_text(encoding="utf-8")
        self.assertIn("sudo -n -l", vl_sh)
        self.assertIn("cchouse-verify/cchouse-power", vl_sh)

    def test_check_page_retries_and_preserves_mismatches(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js unavailable for check.html regression test")
        script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const ip = "203.0.113.10", timezone = "America/Los_Angeles", asn = "AS64500";
const primary = {ip, timezone, org: asn + " Example"};
const fallback = {ip, location: {timezone}, isp: {asn, org: "Example"}};
const html = fs.readFileSync("guest-linux/check.html", "utf8");
assert.match(html, /id="verdict" class="pending" role="status">正在检查网络…</);
const template = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replaceAll("<EXPECTED_EGRESS_IP>", ip)
  .replaceAll("<TIMEZONE>", timezone)
  .replaceAll("<EXPECTED_EGRESS_ASN>", asn);

async function check(fixtures) {
  const verdict = {className: "pending"};
  const calls = {ipinfo: 0, ipquery: 0, ipify: 0}, order = [], waits = [];
  const rows = {insertRow: () => ({insertCell: () => ({appendChild() {}})})};
  await vm.runInNewContext(template, {
    AbortSignal, performance,
    document: {
      getElementById: id => id === "rows" ? rows : verdict,
      createElement: () => ({}),
    },
    fetch: async (url, options) => {
      const key = Object.keys(calls).find(name => url.includes(name));
      assert.ok(options.signal);
      order.push(key);
      const value = fixtures[key][calls[key]++];
      if (value instanceof Error) throw value;
      return {ok: value !== undefined, json: async () => value};
    },
    setTimeout: (action, delay) => {
      assert.equal(verdict.className, "pending");
      waits.push(delay);
      action();
    },
    navigator: {language: "en-US", languages: ["en-US"]},
    Intl: {DateTimeFormat: () => ({resolvedOptions: () => ({timeZone: timezone})})},
  });
  return {status: verdict.className, text: verdict.textContent, order, waits};
}

(async () => {
  const offline = new TypeError("network unavailable");
  const fallbackSuccess = await check({ipquery: [offline], ipinfo: [primary], ipify: [{ip}]});
  assert.equal(fallbackSuccess.status, "ok");
  assert.deepEqual(fallbackSuccess.order, ["ipquery", "ipinfo", "ipify"]);
  const retried = await check({ipquery: [offline, fallback], ipinfo: [], ipify: [offline, {ip}]});
  assert.equal(retried.status, "ok");
  assert.deepEqual(retried.order, ["ipquery", "ipinfo", "ipify", "ipquery", "ipify"]);
  assert.deepEqual(retried.waits, [2000]);
  for (const wrong of [{...fallback, ip: "203.0.113.11"}, {...fallback, location: {timezone: "UTC"}}]) {
    const mismatch = await check({ipquery: [wrong], ipinfo: [primary], ipify: [{ip}]});
    assert.equal(mismatch.status, "bad");
    assert.deepEqual(mismatch.order, ["ipquery", "ipify"]);
  }
  const ipOnly = await check({ipquery: [], ipinfo: [], ipify: [{ip}]});
  assert.equal(ipOnly.status, "bad");
  assert.match(ipOnly.text, /稍后刷新或运行 verify/);
  assert.deepEqual(ipOnly.waits, [2000, 4000, 8000]);
  assert.equal(ipOnly.order.filter(name => name === "ipify").length, 1);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [node, "-"], input=script, cwd=ROOT, capture_output=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ustc_mirror_is_removed_before_golden_image_assertions(self):
        import re

        bash = find_bash()
        if bash is None:
            self.skipTest("bash unavailable")
        builder = ROOT / "guest-linux" / "image-builder"
        tart = (builder / "build-tart-linux-from-cloud-image.sh").read_text(encoding="utf-8")
        golden = (builder / "build-debian13-golden.sh").read_text(encoding="utf-8")
        assertions = (builder / "assert-empty-state.sh").read_text(encoding="utf-8")
        self.assertIn('BUILD_CMD="USE_USTC_MIRROR=${USE_USTC_MIRROR}', tart)
        rewrite = re.search(r"^rewrite_apt_sources\(\) \{\n.*?^\}$", golden, re.M | re.S)
        check = re.search(r"^apt_without_ustc\(\) \{\n.*?^\}$", assertions, re.M | re.S)
        self.assertIsNotNone(rewrite)
        self.assertIsNotNone(check)
        enable, restore = re.findall(r"^\s*rewrite_apt_sources /etc/apt ('.*')$", golden, re.M)
        self.assertLess(golden.index(enable), golden.index("apt-get update"))
        self.assertLess(golden.index("apt-get clean"), golden.index(restore))
        self.assertLess(golden.index(restore), golden.index('bash "${SCRIPT_DIR}/assert-empty-state.sh"'))
        self.assertIn("apt_without_ustc /etc/apt", assertions)

        with tempfile.TemporaryDirectory() as td:
            apt_dir = Path(td) / "apt with spaces"
            (apt_dir / "sources.list.d").mkdir(parents=True)
            (apt_dir / "mirrors").mkdir()
            (apt_dir / "apt.conf.d").mkdir()
            packages = apt_dir / "mirrors" / "debian.list"
            packages.write_text("https://deb.debian.org/debian\n", encoding="utf-8")
            security = apt_dir / "sources.list.d" / "security.sources"
            security.write_text("URIs: https://deb.debian.org/debian-security\n", encoding="utf-8")
            residue = apt_dir / "apt.conf.d" / "99-unused"
            script = "\n".join((
                "set -euo pipefail",
                rewrite.group(0),
                check.group(0),
                'case "$1" in',
                f'  enable) rewrite_apt_sources "$2" {enable} ;;',
                f'  restore) rewrite_apt_sources "$2" {restore} ;;',
                '  check) apt_without_ustc "$2" ;;',
                "esac",
            ))
            harness = Path(td) / "apt-mirror.sh"
            harness.write_text(script + "\n", encoding="utf-8")

            def apply(action):
                return subprocess.run(
                    [bash, str(harness), action, apt_dir.as_posix()],
                    capture_output=True, text=True,
                )

            result = apply("enable")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(packages.read_text(), "https://mirrors.ustc.edu.cn/debian\n")
            self.assertEqual(security.read_text(), "URIs: https://mirrors.ustc.edu.cn/debian-security\n")
            self.assertNotEqual(apply("check").returncode, 0)
            result = apply("restore")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(packages.read_text(), "https://deb.debian.org/debian\n")
            self.assertEqual(security.read_text(), "URIs: https://security.debian.org/debian-security\n")
            self.assertEqual(apply("check").returncode, 0)
            residue.write_text("# MIRRORS.USTC.EDU.CN\n", encoding="utf-8")
            self.assertNotEqual(apply("check").returncode, 0)

    def test_builders_do_not_background_golden_chain_and_write_release_meta(self):
        import re

        tart_sh = (ROOT / "guest-linux" / "image-builder" / "build-tart-linux-from-cloud-image.sh").read_text(encoding="utf-8")
        ova_ps1 = (ROOT / "guest-linux" / "image-builder" / "build-ova-from-cloud-image.ps1").read_text(encoding="utf-8")
        golden_sh = (ROOT / "guest-linux" / "image-builder" / "build-debian13-golden.sh").read_text(encoding="utf-8")

        tart_cmd_m = re.search(r'^BUILD_CMD="(.+)"$', tart_sh, re.MULTILINE)
        ova_cmd_m = re.search(r'^\$buildCmd\s*=\s*"(.+)"$', ova_ps1, re.MULTILINE)
        self.assertIsNotNone(tart_cmd_m, "未找到 tart 构建器 BUILD_CMD")
        self.assertIsNotNone(ova_cmd_m, "未找到 ova 构建器 $buildCmd")

        for name, cmd in (("tart", tart_cmd_m.group(1)), ("ova", ova_cmd_m.group(1))):
            self.assertIn("build-debian13-golden.sh", cmd, name)
            self.assertIn("SOURCE_COMMIT=", cmd, name)
            self.assertIn("BUILD_TIME=", cmd, name)
            # poweroff 之前的 && 链不得放在后台，只有关机动作被花括号包在后台
            self.assertNotRegex(cmd, r"&&\s*nohup\b", f"{name}: 整条构建链被放进后台")
            self.assertRegex(
                cmd,
                r"&&\s*\{\s*nohup\s+sh\s+-c\s+'sleep 2 && systemctl poweroff'[^}]*&\s*\}\s*$",
                f"{name}: 未将关机单独隔离在后台花括号块中",
            )

        # 验证 golden.sh 写入 /etc/cchouse-image-release
        self.assertIn("/etc/cchouse-image-release", golden_sh)
        for field in ("SOURCE_COMMIT=", "BUILD_TIME=", "ARCH=", "HYPERVISOR="):
            self.assertIn(field, golden_sh)

        # 验证 tart 构建器在临时名上构建、成功后 rename 成正式名、失败只删临时名
        self.assertIn('BUILD_VM_NAME="${VM_NAME}-building"', tart_sh)
        self.assertIn('"$TART_BIN" rename "$BUILD_VM_NAME" "$VM_NAME"', tart_sh)
        self.assertIn('"$TART_BIN" delete "$BUILD_VM_NAME"', tart_sh)
        self.assertNotIn(".cchouse-golden-ok", tart_sh)

        # 验证两个构建器的 -dirty 判定均忽略未跟踪文件
        self.assertIn("status --porcelain --untracked-files=no", tart_sh)
        self.assertIn("status --porcelain --untracked-files=no", ova_ps1)

        # 动态验证：golden 失败时前置链退出码不会被后台关机吞掉
        bash_exe = find_bash()
        if bash_exe:
            cp = subprocess.run(
                [bash_exe, "-c", "false && sync && { nohup sh -c 'sleep 1 && true' </dev/null >/dev/null 2>&1 & }"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(cp.returncode, 0)

    def test_manifest_schema_v2_and_preflight_alignment(self):
        import re

        sys.path.insert(0, str(ROOT / "scripts"))
        import preflight as preflight_mod

        manifest = json.loads((ROOT / "guest-linux" / "image-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("schema_version"), 2)
        self.assertTrue(isinstance(manifest.get("guest_os"), str) and manifest["guest_os"])
        img_ver = manifest.get("image_version")
        self.assertTrue(isinstance(img_ver, str) and img_ver.startswith("v"))

        artifacts = manifest.get("artifacts")
        self.assertIsInstance(artifacts, list)
        routes = [a.get("route") for a in artifacts]
        self.assertEqual(sorted(routes), ["tart-linux", "virtualbox-linux"])

        required_fields = (
            "arch",
            "route",
            "format",
            "filename",
            "size_bytes",
            "urls",
            "sha256",
            "source_commit",
            "included_components",
        )
        for art in artifacts:
            for key in required_fields:
                self.assertIn(key, art, f"artifact 缺少必填字段 {key}")
            self.assertIsInstance(art["size_bytes"], int)
            self.assertGreater(art["size_bytes"], 0)
            self.assertRegex(art["sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(art["source_commit"], r"^[0-9a-f]{7,40}$")
            self.assertIsInstance(art["urls"], list)
            self.assertGreater(len(art["urls"]), 0)
            for u in art["urls"]:
                self.assertTrue(u.endswith("/" + art["filename"]), f"URL 文件名与 filename 不一致: {u}")
                self.assertIn(f"/{img_ver}/", u, f"URL 版本标签与 image_version 不一致: {u}")
            self.assertIsInstance(art["included_components"], list)
            self.assertGreater(len(art["included_components"]), 0)

            exp = preflight_mod.expected_image_for_route(art["route"])
            self.assertIsNotNone(exp)
            self.assertEqual(exp["image_version"], img_ver)
            self.assertEqual(exp["filename"], art["filename"])
            self.assertEqual(exp["sha256"], art["sha256"])
            self.assertEqual(exp["source_commit"], art["source_commit"])
            self.assertEqual(exp["url"], art["urls"][0])

        self.assertIsNone(preflight_mod.expected_image_for_route("macos-tart"))

    def test_p2_empty_keyboard_and_sudo_power_audit(self):
        with tempfile.TemporaryDirectory() as td:
            # 1) 空 GUEST_KEYBOARD 在 preflight 阶段即报错
            empty_kb_env = base_single_linux_env()
            empty_kb_env["GUEST_KEYBOARD"] = ""
            env_p = Path(td) / "empty_kb.env"
            write_env(env_p, empty_kb_env)
            cp = subprocess.run(
                [sys.executable, "-I", str(PREFLIGHT), "--route", "tart-linux", "--env", str(env_p)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("INVALID_GUEST_KEYBOARD", cp.stdout)

        # 2) verify-linux.sh 的 sudo 审计在缺少 cchouse-power 规则时判 FAIL
        vl_sh = (ROOT / "scripts" / "verify-linux.sh").read_text(encoding="utf-8")
        awk_prog = vl_sh.split("sudo -n -l 2>/dev/null | awk '", 1)[1].split("'\" 2>/dev/null", 1)[0]
        awk_prog = awk_prog.replace('\\"', '"').replace("\\$", "$")
        awk_exe = shutil.which("awk")
        if awk_exe:
            verify_only = (
                "User claude may run the following commands on host:\n"
                "    (root) NOPASSWD: /usr/local/sbin/cchouse-probe nft-loaded\n"
                "    (root) NOPASSWD: /usr/local/sbin/cchouse-probe nft-live-sha\n"
                "    (root) NOPASSWD: /usr/local/sbin/cchouse-probe direct-count\n"
                "    (root) NOPASSWD: /usr/local/sbin/cchouse-probe mihomo-stop\n"
                "    (root) NOPASSWD: /usr/local/sbin/cchouse-probe mihomo-start\n"
                "    (root) NOPASSWD: /usr/local/sbin/cchouse-probe root-egress\n"
            )
            with_power = verify_only + (
                "    (root) NOPASSWD: /usr/bin/systemctl poweroff\n"
                "    (root) NOPASSWD: /usr/bin/systemctl reboot\n"
            )
            with_wildcard = with_power + "    (root) NOPASSWD: /usr/bin/systemctl *\n"

            for sample, expect_ok in ((verify_only, False), (with_power, True), (with_wildcard, False)):
                r = subprocess.run([awk_exe, awk_prog], input=sample, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                if expect_ok:
                    self.assertEqual(r.stdout.strip(), "ok")
                else:
                    self.assertNotEqual(r.stdout.strip(), "ok")

    def test_docs_keyboard_and_version_consistency(self):
        for doc_name in ("install-tart-linux.md", "install-virtualbox-linux.md"):
            text = (ROOT / "docs" / doc_name).read_text(encoding="utf-8")
            self.assertNotIn("按出口地区配置键盘布局", text, doc_name)
            self.assertIn("GUEST_KEYBOARD", text, doc_name)
            self.assertIn("v2.0.0-preview.2", text, doc_name)

    def test_firstboot_atomic_sync_and_empty_state_output(self):
        # 1) firstboot.sh 安装 Claude Code 采用同目录临时文件 + sync + 原子 mv，并在末尾 sync 落盘
        fb = (ROOT / "guest-linux" / "firstboot.sh").read_text(encoding="utf-8")
        self.assertIn("/usr/local/bin/.claude.tmp", fb)
        self.assertIn("mv -f /usr/local/bin/.claude.tmp /usr/local/bin/claude", fb)
        self.assertRegex(fb, r"(?m)^sync\s*$")

        # 2) cchouse-firstboot.service 显式声明 Wants=network-pre.target
        svc = (ROOT / "guest-linux" / "cchouse-firstboot.service").read_text(encoding="utf-8")
        self.assertIn("Before=network-pre.target", svc)
        self.assertIn("Wants=network-pre.target", svc)

        # 3) assert-empty-state.sh 不得在 check 外层重定向 stdout/stderr（避免静默吞掉 PASS/FAIL 输出）
        aes = (ROOT / "guest-linux" / "image-builder" / "assert-empty-state.sh").read_text(encoding="utf-8")
        for line in aes.splitlines():
            stripped = line.strip()
            if stripped.startswith('check "'):
                self.assertFalse(
                    stripped.endswith(">/dev/null 2>&1") or stripped.endswith(">/dev/null"),
                    f"assert-empty-state.sh 中 check 外层重定向会吞掉 PASS/FAIL 输出：{stripped}",
                )
        self.assertIn("bash -c 'systemctl is-enabled cchouse-default-drop.service >/dev/null 2>&1'", aes)
        self.assertIn("bash -c 'systemctl is-enabled cchouse-firstboot.service >/dev/null 2>&1'", aes)

        # 4) build-tart-linux-from-cloud-image.sh 使用唯一临时主机名并在 ARP 未命中时回退 /var/db/dhcpd_leases
        tart_builder = (ROOT / "guest-linux" / "image-builder" / "build-tart-linux-from-cloud-image.sh").read_text(encoding="utf-8")
        self.assertIn("BUILD_HOST=", tart_builder)
        self.assertIn("/var/db/dhcpd_leases", tart_builder)

    def test_firstboot_publishes_ed25519_hostkey_on_serial(self):
        fb = (ROOT / "guest-linux" / "firstboot.sh").read_text(encoding="utf-8")
        publish = fb.split("ssh-keygen -A", 1)[1].split("GUEST_HOSTNAME", 1)[0]
        self.assertIn("CCHOUSE_HOSTKEY", publish)
        self.assertIn("ssh_host_ed25519_key.pub", publish)
        self.assertIn("timeout 2", publish)
        self.assertIn("/dev/ttyS0", publish)
        self.assertNotIn("ssh_host_ed25519_key >/dev/ttyS0", publish)
        self.assertNotIn("cat ", publish)

    def test_linux_single_optional_stripe_node_full_chain(self):
        with tempfile.TemporaryDirectory() as td:
            # 1) EXIT_MODE=single + STRIPE_EXIT_MODE=node 全链路（config.yaml + nftables.conf + preflight）
            data = base_single_linux_env()
            data.update({
                "STRIPE_EXIT_MODE": "node",
                "STRIPE_NODE_TYPE": "vless",
                "STRIPE_NODE_SERVER": "stripe-edge.example.com",
                "STRIPE_NODE_SERVER_IP": "203.0.113.88",
                "STRIPE_NODE_PORT": "443",
                "STRIPE_NODE_UUID": "demo-stripe-uuid",
                "STRIPE_NODE_FINGERPRINT": "chrome",
            })
            env_f = Path(td) / "stripe_node.env"
            out = Path(td) / "out_stripe"
            write_env(env_f, data)
            cp = subprocess.run([sys.executable, "-I", str(RENDER), str(env_f), str(out)], capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(cp.returncode, 0, cp.stderr)
            cfg = (out / "config.yaml").read_text(encoding="utf-8")
            nft = (out / "nftables.conf").read_text(encoding="utf-8")
            self.assertIn("- name: WIN01-Single-Exit", cfg)
            self.assertIn("- name: WIN01-Stripe-Node", cfg)
            self.assertIn("server: 203.0.113.88", cfg)
            self.assertIn("servername: stripe-edge.example.com", cfg)
            self.assertIn("- name: WIN01-Stripe\n  type: select\n  proxies:\n  - WIN01-Stripe-Node\n  disable-udp: true", cfg)
            self.assertIn("- DOMAIN-SUFFIX,stripe.com,WIN01-Stripe", cfg)
            self.assertIn("- DOMAIN-SUFFIX,stripe.network,WIN01-Stripe", cfg)
            self.assertIn("- DOMAIN-SUFFIX,stripecdn.com,WIN01-Stripe", cfg)
            self.assertIn("- NETWORK,UDP,REJECT", cfg)
            self.assertIn("- MATCH,WIN01-Exit", cfg)
            self.assertNotIn(",DIRECT", cfg)
            non_comment_nft = "\n".join(l for l in nft.splitlines() if not l.lstrip().startswith("#"))
            self.assertNotIn("flush ruleset", non_comment_nft)
            self.assertIn('meta skuid "mihomo" ip daddr 203.0.113.55 tcp dport 1080 accept', non_comment_nft)
            self.assertIn('meta skuid "mihomo" ip daddr 203.0.113.88 tcp dport 443 accept', non_comment_nft)

            # 2) 省略 STRIPE_EXIT_MODE 但填了 STRIPE_NODE_SERVER 时自动按 node 渲染
            implicit_data = dict(data)
            del implicit_data["STRIPE_EXIT_MODE"]
            env_imp = Path(td) / "implicit.env"
            out_imp = Path(td) / "out_imp"
            write_env(env_imp, implicit_data)
            cp_imp = subprocess.run([sys.executable, "-I", str(RENDER), str(env_imp), str(out_imp)], capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(cp_imp.returncode, 0, cp_imp.stderr)
            self.assertEqual((out_imp / "config.yaml").read_text(encoding="utf-8"), cfg)

            # 3) STRIPE_NODE 拒绝 UDP 协议
            udp_data = dict(data)
            udp_data["STRIPE_NODE_TYPE"] = "hysteria2"
            env_udp = Path(td) / "stripe_udp.env"
            write_env(env_udp, udp_data)
            cp_udp = subprocess.run([sys.executable, "-I", str(RENDER), str(env_udp), str(Path(td) / "out_udp")], capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertNotEqual(cp_udp.returncode, 0)
            self.assertIn("UDP", cp_udp.stderr)

            # 4) EXIT_MODE=single 拒绝 STRIPE_EXIT_MODE=front
            front_data = base_single_linux_env()
            front_data["STRIPE_EXIT_MODE"] = "front"
            env_fr = Path(td) / "front.env"
            write_env(env_fr, front_data)
            cp_fr = subprocess.run([sys.executable, "-I", str(RENDER), str(env_fr), str(Path(td) / "out_fr")], capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertNotEqual(cp_fr.returncode, 0)
            cp_pf = subprocess.run([sys.executable, "-I", str(PREFLIGHT), "--route", "tart-linux", "--env", str(env_fr)], capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertNotEqual(cp_pf.returncode, 0)
            self.assertIn("INVALID_STRIPE_MODE", cp_pf.stdout)

            # 5) STRIPE_EXIT_MODE=off 但填了 STRIPE_NODE_SERVER 时 preflight 给出 STRIPE_NODE_IGNORED 警告
            ign_data = dict(data)
            ign_data["STRIPE_EXIT_MODE"] = "off"
            env_ign = Path(td) / "ign.env"
            write_env(env_ign, ign_data)
            cp_ign = subprocess.run([sys.executable, "-I", str(PREFLIGHT), "--route", "tart-linux", "--env", str(env_ign)], capture_output=True, text=True, encoding="utf-8", errors="replace")
            payload_ign = json.loads(cp_ign.stdout)
            self.assertTrue(any("STRIPE_NODE_IGNORED" in w for w in payload_ign["warnings"]))

    def test_stripe_probe_unreachable_proxy_does_not_crash(self):
        import socket as _socket
        sys.path.insert(0, str(ROOT / "scripts"))
        import preflight as preflight_mod
        with _socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            dead_port = srv.getsockname()[1]
        for fn in (preflight_mod._probe_socks5_connect, preflight_mod._probe_http_connect):
            ok_flag, detail = fn("127.0.0.1", dead_port, "u", "p", "js.stripe.com", timeout=2)
            self.assertFalse(ok_flag)
            self.assertTrue(detail.startswith("proxy_connect_error("), detail)

    def test_preflight_check_stripe_and_verify_linux_markers(self):
        import socket
        import threading

        def start_mock_socks5(block_stripe):
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 0))
            srv.listen(8)
            stop = threading.Event()

            def serve():
                srv.settimeout(0.2)
                while not stop.is_set():
                    try:
                        conn, _ = srv.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    try:
                        hdr = conn.recv(4)
                        if not hdr or hdr[0] != 5:
                            conn.close()
                            continue
                        # 要求用户名/密码认证 0x02
                        conn.sendall(b"\x05\x02")
                        auth = conn.recv(256)
                        if not auth:
                            conn.close()
                            continue
                        conn.sendall(b"\x01\x00")
                        req = conn.recv(256)
                        if len(req) >= 5 and req[3] == 3:
                            dlen = req[4]
                            dom = req[5:5 + dlen].decode("ascii", errors="ignore")
                            rep = 0x02 if (block_stripe and dom.endswith("stripe.com")) else 0x00
                            conn.sendall(bytes([5, rep, 0, 1, 0, 0, 0, 0, 0, 0]))
                    finally:
                        conn.close()

            t = threading.Thread(target=serve, daemon=True)
            t.start()
            return srv, stop, srv.getsockname()[1]

        srv_blk, stop_blk, port_blk = start_mock_socks5(block_stripe=True)
        srv_ok, stop_ok, port_ok = start_mock_socks5(block_stripe=False)
        try:
            with tempfile.TemporaryDirectory() as td:
                # 1) single + off 遇到屏蔽 Stripe 的 SOCKS5 -> 明确 STRIPE_UNREACHABLE 警告且不泄漏密码
                d1 = base_single_linux_env()
                d1.update({
                    "SINGLE_SERVER": "127.0.0.1",
                    "SINGLE_PORT": str(port_blk),
                    "SINGLE_PASS": "SECRET_STRIPE_TEST_PASS",
                })
                e1 = Path(td) / "blk.env"
                write_env(e1, d1)
                cp1 = subprocess.run(
                    [sys.executable, "-I", str(PREFLIGHT), "--route", "tart-linux", "--env", str(e1), "--check-stripe"],
                    capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                )
                self.assertNotIn("SECRET_STRIPE_TEST_PASS", cp1.stdout + cp1.stderr)
                p1 = json.loads(cp1.stdout)
                self.assertIsNotNone(p1["stripe_check"])
                self.assertFalse(p1["stripe_check"]["reachable"])
                self.assertTrue(any("STRIPE_UNREACHABLE" in w and "付款会失败" in w for w in p1["warnings"]))

                # 2) single + node 指向屏蔽 Stripe 的节点 -> STRIPE_NODE_UNREACHABLE 仅警告，不算错误
                d2 = dict(d1)
                d2.update({
                    "STRIPE_EXIT_MODE": "node",
                    "STRIPE_NODE_TYPE": "socks5",
                    "STRIPE_NODE_SERVER": "127.0.0.1",
                    "STRIPE_NODE_PORT": str(port_blk),
                    "STRIPE_NODE_USER": "su",
                    "STRIPE_NODE_PASS": "sp",
                })
                e2 = Path(td) / "node_blk.env"
                write_env(e2, d2)
                cp2 = subprocess.run(
                    [sys.executable, "-I", str(PREFLIGHT), "--route", "tart-linux", "--env", str(e2), "--check-stripe"],
                    capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                )
                p2 = json.loads(cp2.stdout)
                self.assertTrue(any("STRIPE_NODE_UNREACHABLE" in w for w in p2["warnings"]))
                self.assertFalse(any("STRIPE" in e for e in p2["errors"]))

                # 3) single + node 指向可达 Stripe 的节点 -> stripe_check.reachable=True，无 Stripe 错误/警告
                d3 = dict(d2)
                d3["STRIPE_NODE_PORT"] = str(port_ok)
                e3 = Path(td) / "node_ok.env"
                write_env(e3, d3)
                cp3 = subprocess.run(
                    [sys.executable, "-I", str(PREFLIGHT), "--route", "tart-linux", "--env", str(e3), "--check-stripe"],
                    capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                )
                p3 = json.loads(cp3.stdout)
                self.assertTrue(p3["stripe_check"]["reachable"])
                self.assertFalse(any("STRIPE_" in e for e in p3["errors"]))
                self.assertFalse(any("STRIPE_" in w for w in p3["warnings"]))
        finally:
            stop_blk.set()
            stop_ok.set()
            srv_blk.close()
            srv_ok.close()

        # 4) verify-linux.sh 含 Stripe 可达性检查且语法正确
        vl_sh = (ROOT / "scripts" / "verify-linux.sh").read_text(encoding="utf-8")
        for marker in ("Stripe 可达性", "https://js.stripe.com/v3/", "https://api.stripe.com/v1", "付款会失败", "stripe_reachable"):
            self.assertIn(marker, vl_sh)
        bash_exe = find_bash()
        if bash_exe:
            chk = subprocess.run([bash_exe, "-n", str(ROOT / "scripts" / "verify-linux.sh")], capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(chk.returncode, 0, chk.stderr)

    def test_cloud_ova_workflow_and_ovf_template(self):
        import xml.etree.ElementTree as ET

        wf = (ROOT / ".github" / "workflows" / "build-ova.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", wf)
        self.assertNotIn("\n  pull_request:", wf)
        self.assertNotIn("\n  push:", wf)
        for marker in (
            "/dev/kvm",
            "build-debian13-golden.sh",
            "subformat=streamOptimized",
            "tar --format=ustar",
            "actions/upload-artifact@v4",
            "cchouse-debian13-amd64.ovf.tmpl",
            "guest-linux/virtualbox-manifest.json",
            "VBOX_GUEST_ADDITIONS_ISO=/tmp/VBoxGuestAdditions.iso",
            "VBOX_GUEST_ADDITIONS_SHA256=${GA_SHA256}",
            "拒绝 VirtualBox 附加包",
        ):
            self.assertIn(marker, wf)
        self.assertNotIn("gh release", wf)
        self.assertNotIn("Oracle_VirtualBox_Extension_Pack", wf)
        self.assertNotIn(".vbox-extpack", wf)

        ovf_text = (ROOT / "guest-linux" / "image-builder" / "cchouse-debian13-amd64.ovf.tmpl").read_text(encoding="utf-8")
        rendered = (
            ovf_text.replace("__VMDK_FILE__", "cchouse-debian13-amd64-disk1.vmdk")
            .replace("__VMDK_SIZE__", "123456")
            .replace("__DISK_CAPACITY__", "25769803776")
        )
        root = ET.fromstring(rendered)
        ns = {
            "ovf": "http://schemas.dmtf.org/ovf/envelope/1",
            "rasd": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData",
        }
        disk = root.find(".//ovf:Disk", ns)
        self.assertIsNotNone(disk)
        self.assertIn("streamOptimized", disk.attrib.get(f"{{{ns['ovf']}}}format", ""))
        nic_items = [
            it for it in root.findall(".//ovf:Item", ns)
            if (it.findtext("rasd:ResourceType", default="", namespaces=ns) == "10")
        ]
        self.assertEqual(len(nic_items), 1)
        self.assertEqual(nic_items[0].findtext("rasd:ResourceSubType", default="", namespaces=ns), "VmxNet3")
        self.assertEqual(nic_items[0].findtext("rasd:AutomaticAllocation", default="", namespaces=ns), "false")

    def test_cloud_arm64_tart_oci_workflow_and_packer(self):
        import importlib.util
        from types import SimpleNamespace

        wf = (ROOT / ".github" / "workflows" / "build-tvm.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", wf)
        self.assertNotIn("\n  pull_request:", wf)
        self.assertNotIn("\n  push:", wf)
        for marker in (
            "runs-on: ubuntu-24.04-arm",
            "packages: write",
            "/dev/kvm",
            "build-debian13-golden.sh",
            "TARGET_HYPERVISOR=tart",
            "MIHOMO_ARCH=arm64",
            "push-tart-oci.py",
        ):
            self.assertIn(marker, wf)

        spec = importlib.util.spec_from_file_location(
            "push_tart_oci",
            ROOT / "guest-linux" / "image-builder" / "push-tart-oci.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        nvram = mod.empty_linux_nvram_bytes()
        self.assertEqual(len(nvram), 131072)
        self.assertEqual(nvram[40:44], b"_FVH")
        self.assertEqual(
            mod.parse_image_ref("ghcr.io/nana-fox/cchouse-debian13-arm64:ci-test"),
            ("ghcr.io", "nana-fox/cchouse-debian13-arm64", "ci-test"),
        )
        with self.assertRaises(ValueError):
            mod.parse_image_ref("ghcr.io/Nana-Fox/cchouse-debian13-arm64:ci-test")

        # Dry-run manifest generation with a tiny synthetic disk (mocking bv4 compressor for stdlib-only CI)
        orig_comp = mod.compress_apple_lz4_bv4
        mod.compress_apple_lz4_bv4 = lambda b: b"bv4-" + len(b).to_bytes(4, "little") + b + b"bv4$"
        try:
            with tempfile.TemporaryDirectory() as td:
                disk_p = Path(td) / "disk.raw"
                disk_p.write_bytes(b"\x00" * 4096)
                man_p = Path(td) / "manifest.json"
                res = mod.build_and_push(
                    SimpleNamespace(
                        disk=str(disk_p),
                        image="ghcr.io/nana-fox/cchouse-debian13-arm64:ci-test",
                        cpu=4,
                        memory_mb=4096,
                        display="1440x900",
                        dry_run=True,
                        manifest_out=str(man_p),
                    )
                )
                self.assertEqual(res["uncompressed_disk_bytes"], 4096)
                manifest = json.loads(man_p.read_text(encoding="utf-8"))
                media_types = [l["mediaType"] for l in manifest["layers"]]
                self.assertEqual(
                    media_types,
                    [
                        "application/vnd.cirruslabs.tart.config.v1",
                        "application/vnd.cirruslabs.tart.disk.v2",
                        "application/vnd.cirruslabs.tart.nvram.v1",
                    ],
                )
                self.assertEqual(manifest["annotations"]["org.cirruslabs.tart.uncompressed-disk-size"], "4096")
                self.assertEqual(manifest["layers"][1]["annotations"]["org.cirruslabs.tart.uncompressed-size"], "4096")
        finally:
            mod.compress_apple_lz4_bv4 = orig_comp

    def test_shell_vars_braced_before_non_ascii(self):
        # macOS /bin/bash 3.2 在 UTF-8 locale 下会把紧跟的多字节字符首字节并入变量名（"$x）" -> 变量 x\xef），
        # set -u 时直接报 unbound variable；凡变量后紧跟非 ASCII 字符必须写成 ${x}
        import re
        pat = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[^\x00-\x7f])")
        offenders = []
        for sh in sorted(ROOT.rglob("*.sh")):
            if ".git" in sh.parts:
                continue
            for no, line in enumerate(sh.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    offenders.append(f"{sh.relative_to(ROOT)}:{no}")
        self.assertEqual(offenders, [])
    @unittest.skipIf(os.name == "nt", "requires POSIX executable paths")
    def test_verify_linux_cuts_off_a_stalled_ssh_process(self):
        import re
        bash = find_bash()
        if not bash:
            self.skipTest("需要 POSIX bash")
        src = (ROOT / "scripts" / "verify-linux.sh").read_text(encoding="utf-8")
        bounded = re.search(r"^run_ssh_probe\(\) \{\n.*?^\}", src, re.S | re.M)
        self.assertIsNotNone(bounded)
        harness = "\n".join([
            "PY_CMD=fixture_python",
            'fixture_python() { "$TEST_PYTHON" "$@"; }',
            bounded.group(),
            """run_ssh_probe 0.2 "$TEST_PYTHON" -c 'import time; time.sleep(30)'""",
        ])
        result = subprocess.run(
            [bash, "-c", harness], capture_output=True, text=True, encoding="utf-8", timeout=5,
            env=dict(os.environ, TEST_PYTHON=sys.executable),
        )
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn("SSH probe exceeded", result.stderr)

    def test_verify_linux_cloudflare_loc_retry(self):
        # Cloudflare 国家探测：只在取不到 loc 时最多重试 2 次；国家不符不重试、照常 FAIL；三次都取不到时说明每次原因
        bash = find_bash()
        if not bash or os.name == "nt":
            self.skipTest("需要 POSIX bash")
        import re
        src = (ROOT / "scripts" / "verify-linux.sh").read_text(encoding="utf-8")
        funcs = []
        for name in ("trace_field", "cf_trace_loc"):
            # 单行函数到行尾 " }"，多行函数到行首 "}"
            m = re.search(r"^%s\(\) \{(?: [^\n]*? \}$|\n.*?^\}$)" % name, src, re.S | re.M)
            self.assertIsNotNone(m, name)
            funcs.append(m.group(0))
        self.assertIn('if cf_trace_loc; then\n    expect "Cloudflare 看到的国家" "$CF_LOC" "$EXPECTED_EGRESS_LOC"', src)
        harness = "\n".join([
            "set -uo pipefail",
            "TRACE=https://example.invalid/trace",
            "CF_TRACE_RETRY_SLEEP=0",
            *funcs,
            'guest() {',
            '  local n; n=$(( $(cat "$CNT") + 1 )); echo "$n" > "$CNT"',
            '  case "$(sed -n "${n}p" "$SCEN")" in',
            '    TIMEOUT) echo "__curl_rc=28" ;;',
            '    EMPTY) echo "__curl_rc=0" ;;',
            '    SSHFAIL) return 255 ;;',
            '    LOC:*) l="$(sed -n "${n}p" "$SCEN")"; printf "fl=1\\nloc=%s\\n__curl_rc=0\\n" "${l#LOC:}" ;;',
            '  esac',
            '}',
            'if cf_trace_loc; then echo "OK ${CF_LOC}"; else echo "FAIL ${CF_LOC_REASON}"; fi',
            'echo "calls=$(cat "$CNT")"',
        ])
        cases = [
            (["EMPTY", "TIMEOUT", "LOC:US"], "OK US", 3),
            (["LOC:JP"], "OK JP", 1),
            (["TIMEOUT", "TIMEOUT", "TIMEOUT"], "FAIL 第1次超时；第2次超时；第3次超时", 3),
            (["EMPTY", "SSHFAIL", "EMPTY", "LOC:US"], "FAIL 第1次空响应；第2次 SSH 无返回；第3次空响应", 3),
        ]
        with tempfile.TemporaryDirectory() as td:
            hp = Path(td) / "h.sh"
            hp.write_text(harness + "\n", encoding="utf-8")
            for scen, want, calls in cases:
                sp = Path(td) / "scen"
                sp.write_text("\n".join(scen) + "\n", encoding="utf-8")
                cp = Path(td) / "cnt"
                cp.write_text("0\n", encoding="utf-8")
                env = dict(os.environ, SCEN=str(sp), CNT=str(cp), LC_ALL="en_US.UTF-8")
                r = subprocess.run([bash, str(hp)], capture_output=True, env=env)
                out = r.stdout.decode("utf-8", "replace").splitlines()
                self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
                self.assertEqual(out, [want, f"calls={calls}"], scen)

    def test_verify_linux_ssh_port_and_system32_openssh(self):
        # VirtualBox NAT：GUEST_IP=127.0.0.1 且 SSH_PORT 不是 22。Git Bash 的 /usr/bin/ssh
        # 会盖住 PATH，所以 Windows 上必须直接执行 System32 的 ssh.exe / scp.exe。
        # System32 ssh 认 -i 的 /c/...，但 UserKnownHostsFile="/c/..." 会被拒绝，要转成 C:/...。
        bash = find_bash()
        if not bash:
            self.skipTest("需要 bash")
        src = (ROOT / "scripts" / "verify-linux.sh").read_text(encoding="utf-8")
        self.assertIn('"/c/Windows/System32/OpenSSH/${_name}.exe"', src)
        self.assertIn('"/mnt/c/Windows/System32/OpenSSH/${_name}.exe"', src)
        self.assertIn("resolve_openssh ssh", src)
        self.assertIn("resolve_openssh scp", src)
        self.assertIn("cygpath -m", src)
        self.assertIn('-p "$SSH_PORT"', src)
        self.assertIn('-P "$SSH_PORT"', src)
        self.assertIn('UserKnownHostsFile=\\"$SSH_KNOWN_HOSTS\\"', src)
        syntax = subprocess.run([bash, "-n", str(ROOT / "scripts" / "verify-linux.sh")], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        def for_bash(path):
            text = str(path)
            if os.name == "nt" and len(text) >= 2 and text[1] == ":":
                return "/" + text[0].lower() + text[2:].replace("\\", "/")
            return text

        def write_verify_env(directory, name, **extra):
            data = base_single_linux_env()
            data.update({
                "GUEST_IP": "127.0.0.1",
                "ROUTER_IP": "192.168.100.1",
                "SSH_KEY": "/c/cchouse/id_ed25519",
                "SSH_KNOWN_HOSTS": "/c/cchouse/Application Support/known_hosts",
            })
            data.update(extra)
            path = directory / name
            write_env(path, data)
            return path

        def tool(path, body):
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

        with tempfile.TemporaryDirectory() as td_raw:
            td = Path(td_raw)
            bindir = td / "bin"
            bindir.mkdir()
            tool(
                bindir / "ssh",
                "#!/bin/bash\nprintf '%s\\n' \"$0\" >> \"$LOG\"\nprintf '%s\\n' \"$*\" >> \"$LOG\"\nexit 0\n",
            )
            tool(
                bindir / "scp",
                "#!/bin/bash\nprintf '%s\\n' \"$0\" >> \"$LOG\"\nprintf '%s\\n' \"$*\" >> \"$LOG\"\nexit 0\n",
            )
            tool(
                bindir / "cygpath",
                "#!/bin/bash\n"
                "if [ \"$1\" != \"-m\" ]; then exit 1; fi\n"
                "p=\"$2\"\n"
                "case \"$p\" in\n"
                "  /c/*) printf 'C:/%s\\n' \"${p#/c/}\" ;;\n"
                "  /mnt/c/*) printf 'C:/%s\\n' \"${p#/mnt/c/}\" ;;\n"
                "  *) printf '%s\\n' \"$p\" ;;\n"
                "esac\n",
            )
            win = td / "OpenSSH"
            win.mkdir()
            tool(
                win / "ssh.exe",
                "#!/bin/bash\nprintf '%s\\n' \"$0\" >> \"$LOG\"\nprintf '%s\\n' \"$*\" >> \"$LOG\"\nexit 0\n",
            )
            tool(
                win / "scp.exe",
                "#!/bin/bash\nprintf '%s\\n' \"$0\" >> \"$LOG\"\nprintf '%s\\n' \"$*\" >> \"$LOG\"\nexit 0\n",
            )
            win_log = td / "win.log"
            path_log = td / "path.log"
            exec_probe = "0" if os.name == "nt" else "1"

            def probe(env_file, *, use_win, use_cygpath, hide_cygpath, log_path, extra_env=None):
                env = os.environ.copy()
                path_entries = [str(bindir)]
                if not use_cygpath:
                    bare = td / "barebin"
                    bare.mkdir(exist_ok=True)
                    for name in ("ssh", "scp"):
                        target = bare / name
                        if not target.exists():
                            target.symlink_to(bindir / name) if os.name != "nt" else target.write_text(
                                (bindir / name).read_text(encoding="utf-8"), encoding="utf-8"
                            )
                            if os.name == "nt":
                                target.chmod(0o755)
                    path_entries = [str(bare)]
                env["PATH"] = os.pathsep.join(path_entries + [env.get("PATH", "")])
                env["LOG"] = for_bash(log_path)
                env["VERIFY_LINUX_TRANSPORT_PROBE"] = "1"
                env["VERIFY_LINUX_TRANSPORT_PROBE_EXEC"] = exec_probe
                env["LC_ALL"] = "en_US.UTF-8"
                if use_win:
                    env["VERIFY_OPENSSH_DIR"] = for_bash(win)
                else:
                    env.pop("VERIFY_OPENSSH_DIR", None)
                if hide_cygpath:
                    env["VERIFY_HIDE_CYGPATH"] = "1"
                else:
                    env.pop("VERIFY_HIDE_CYGPATH", None)
                if extra_env:
                    env.update(extra_env)
                return subprocess.run(
                    [bash, str(ROOT / "scripts" / "verify-linux.sh"), str(env_file)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                )

            converted = probe(
                write_verify_env(td, "port.env", SSH_PORT="2222"),
                use_win=True, use_cygpath=True, hide_cygpath=False, log_path=win_log,
            )
            self.assertEqual(converted.returncode, 86, converted.stderr + converted.stdout)
            self.assertIn("SSH_BIN=", converted.stdout)
            self.assertIn("ssh.exe", converted.stdout.split("SSH_BIN=", 1)[1].splitlines()[0])
            self.assertIn("-i C:/cchouse/id_ed25519", converted.stdout)
            self.assertIn('UserKnownHostsFile="C:/cchouse/Application Support/known_hosts"', converted.stdout)
            self.assertIn("-p 2222", converted.stdout)
            self.assertIn("-P 2222", converted.stdout)
            self.assertNotIn('UserKnownHostsFile="/c/', converted.stdout)
            if exec_probe == "1":
                logged = win_log.read_text(encoding="utf-8")
                self.assertIn("ssh.exe", logged)
                self.assertIn("-p 2222", logged)
                self.assertIn("C:/cchouse/id_ed25519", logged)
                self.assertIn("-P 2222", logged)
                self.assertNotIn(str(bindir / "ssh"), logged)

            kept = probe(
                write_verify_env(td, "keep.env", SSH_PORT="2222"),
                use_win=True, use_cygpath=True, hide_cygpath=True, log_path=td / "keep.log",
            )
            self.assertEqual(kept.returncode, 86, kept.stderr + kept.stdout)
            self.assertIn("-i /c/cchouse/id_ed25519", kept.stdout)
            self.assertIn('UserKnownHostsFile="/c/cchouse/Application Support/known_hosts"', kept.stdout)
            self.assertNotIn("C:/cchouse", kept.stdout)

            system32 = subprocess.run(
                [bash, "-c", "if [ -x /c/Windows/System32/OpenSSH/ssh.exe ] || [ -x /mnt/c/Windows/System32/OpenSSH/ssh.exe ]; then exit 0; fi; exit 1"],
                capture_output=True, text=True,
            )
            if system32.returncode != 0:
                # 没有 System32 ssh 时走 PATH 上的 ssh，即使 cygpath 存在也不改路径。
                plain = probe(
                    write_verify_env(td, "plain.env"),
                    use_win=False, use_cygpath=True, hide_cygpath=False, log_path=path_log,
                )
                self.assertEqual(plain.returncode, 86, plain.stderr + plain.stdout)
                self.assertIn("\nSSH_BIN=ssh\n", "\n" + plain.stdout)
                self.assertIn("-i /c/cchouse/id_ed25519", plain.stdout)
                self.assertIn("-p 22", plain.stdout)
                self.assertIn("-P 22", plain.stdout)
                self.assertNotIn("C:/cchouse", plain.stdout)
                if exec_probe == "1":
                    plain_log = path_log.read_text(encoding="utf-8")
                    self.assertIn("-p 22", plain_log)
                    self.assertNotIn("ssh.exe", plain_log)

            for bad in ("0", "65536", "22abc", "08", "100000"):
                rejected = probe(
                    write_verify_env(td, f"bad-{bad}.env", SSH_PORT=bad),
                    use_win=True, use_cygpath=False, hide_cygpath=True, log_path=td / f"bad-{bad}.log",
                )
                self.assertEqual(rejected.returncode, 1, bad + rejected.stderr)
                self.assertIn("SSH_PORT", rejected.stderr)
                self.assertFalse((td / f"bad-{bad}.log").exists(), bad)

if __name__ == "__main__":
    unittest.main()
