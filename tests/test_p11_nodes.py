import argparse
import base64
import gzip
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import cchouse  # noqa: E402
import doctor as doctor_mod  # noqa: E402
import downloads  # noqa: E402
import preflight as preflight_mod  # noqa: E402
import render as render_mod  # noqa: E402


def _load_verify_native():
    spec = importlib.util.spec_from_file_location("verify_linux_native_p11", ROOT / "scripts" / "verify-linux-native.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


verify_native = _load_verify_native()

SS_CRED = base64.b64encode(b"aes-256-gcm:demo-ss-pass-xyz").decode("ascii")
LINK_SOCKS = "socks5://exit_user:exit_pass_secret@198.51.100.20:8000"
LINK_HTTP = "http://http_user:http_pass_secret@198.51.100.21:8080"
LINK_VLESS = "vless://demo-uuid-secret-1111@203.0.113.10:443?security=reality&sni=example.com&fp=chrome&pbk=demo-pbk-secret&sid=abcd1234&flow=xtls-rprx-vision"
LINK_TROJAN = "trojan://demo-trojan-pass-secret@203.0.113.11:443?sni=trojan.example.com"
LINK_SS = f"ss://{SS_CRED}@203.0.113.12:8388"


def _up_args(td, tag="cc01", route="tart-linux", backend="manual", **over):
    ns = argparse.Namespace(
        tag=tag,
        route=route,
        private_dir=str(Path(td) / "priv" / tag),
        cache_dir=str(Path(td) / "cache"),
        image_mirror=None,
        npm_mirror=None,
        timezone=None,
        expected_ip=None,
        expected_country=None,
        expected_asn=None,
        link_clipboard=False,
        link_file=None,
        link_stdin=False,
        non_interactive=False,
        no_probe=False,
        vm_backend=backend,
        reconfigure=False,
        share=None,
        read_only=False,
        config_only=True,
        skip_preflight=False,
        dry_run=False,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


class TestTwoLinkClassification(unittest.TestCase):
    def test_single_link_all_supported_types(self):
        for link, expected_type in (
            (LINK_SOCKS, "socks5"),
            (LINK_HTTP, "http"),
            (LINK_VLESS, "vless"),
            (LINK_TROJAN, "trojan"),
            (LINK_SS, "ss"),
        ):
            with self.subTest(expected_type=expected_type):
                exit_f, via_f = cchouse.parse_and_classify_links([link])
                self.assertEqual(exit_f["SINGLE_TYPE"], expected_type)
                self.assertIsNone(via_f)

    def test_two_links_encrypted_plus_socks_in_either_order(self):
        for links in ([LINK_VLESS, LINK_SOCKS], [LINK_SOCKS, LINK_VLESS]):
            exit_f, via_f = cchouse.parse_and_classify_links(links)
            self.assertEqual(exit_f["SINGLE_TYPE"], "socks5")
            self.assertEqual(exit_f["SINGLE_SERVER"], "198.51.100.20")
            self.assertIsNotNone(via_f)
            self.assertEqual(via_f["SINGLE_VIA_TYPE"], "vless")
            self.assertEqual(via_f["SINGLE_VIA_SERVER"], "203.0.113.10")

    def test_two_links_trojan_and_ss_as_front(self):
        for enc_link, enc_type in ((LINK_TROJAN, "trojan"), (LINK_SS, "ss")):
            exit_f, via_f = cchouse.parse_and_classify_links([LINK_HTTP, enc_link])
            self.assertEqual(exit_f["SINGLE_TYPE"], "http")
            self.assertEqual(via_f["SINGLE_VIA_TYPE"], enc_type)

    def test_two_links_both_socks_or_http_first_is_front_second_is_exit(self):
        exit_f, via_f = cchouse.parse_and_classify_links([LINK_HTTP, LINK_SOCKS])
        self.assertEqual(via_f["SINGLE_VIA_TYPE"], "http")
        self.assertEqual(via_f["SINGLE_VIA_SERVER"], "198.51.100.21")
        self.assertEqual(exit_f["SINGLE_TYPE"], "socks5")
        self.assertEqual(exit_f["SINGLE_SERVER"], "198.51.100.20")

    def test_two_encrypted_links_rejected_without_leaking(self):
        with self.assertRaises(cchouse.NodeLinkError) as cm:
            cchouse.parse_and_classify_links([LINK_VLESS, LINK_TROJAN])
        msg = str(cm.exception)
        self.assertIn("两条链接都是加密协议", msg)
        for secret in ("demo-uuid-secret", "demo-pbk-secret", "demo-trojan-pass-secret", "203.0.113.10"):
            self.assertNotIn(secret, msg)

    def test_more_than_two_links_rejected_without_leaking(self):
        with self.assertRaises(cchouse.NodeLinkError) as cm:
            cchouse.parse_and_classify_links([LINK_VLESS, LINK_HTTP, LINK_SOCKS])
        msg = str(cm.exception)
        self.assertIn("最多支持 2 条", msg)
        self.assertIn("docs/manual-install.md", msg)
        for secret in ("demo-uuid-secret", "exit_pass_secret", "http_pass_secret"):
            self.assertNotIn(secret, msg)

    def test_interactive_encrypted_first_link_prompts_for_optional_exit(self):
        egress = {"ip": "198.51.100.99", "country": "US", "asn": "AS64500", "timezone": "America/Los_Angeles"}
        # 1) 用户直接回车 -> 单节点 vless
        with tempfile.TemporaryDirectory() as td:
            args = _up_args(td, config_only=True)
            out = io.StringIO()
            with mock.patch.object(cchouse, "_stdin_is_tty", return_value=True), \
                    mock.patch.object(cchouse.getpass, "getpass", side_effect=[LINK_VLESS, ""]) as gp, \
                    mock.patch.object(cchouse, "probe_egress_via_proxy", return_value=egress), \
                    mock.patch.object(cchouse, "run_preflight_check", return_value=(0, {"ok": True, "status": "PREFLIGHT_OK", "next_action": "proceed_to_render", "warnings": [], "errors": []}, "")), \
                    redirect_stdout(out):
                rc = cchouse.cmd_up(args)
            self.assertEqual(rc, 0, out.getvalue())
            self.assertEqual(gp.call_count, 2)
            self.assertIn("如果你的固定 IP 出口是另一条 SOCKS5/HTTP 链接，请粘贴；没有直接回车", gp.call_args_list[1].args[0])
            env_text = (Path(td) / "priv" / "cc01" / "instance.env").read_text(encoding="utf-8")
            self.assertIn("SINGLE_TYPE=vless", env_text)
            self.assertNotIn("SINGLE_VIA_TYPE=", env_text)

        # 2) 用户粘贴第二条 SOCKS5 -> 两跳
        with tempfile.TemporaryDirectory() as td:
            args = _up_args(td, config_only=True)
            out = io.StringIO()
            with mock.patch.object(cchouse, "_stdin_is_tty", return_value=True), \
                    mock.patch.object(cchouse.getpass, "getpass", side_effect=[LINK_VLESS, LINK_SOCKS]), \
                    mock.patch.object(cchouse, "probe_egress_via_proxy", return_value=egress), \
                    mock.patch.object(cchouse, "run_preflight_check", return_value=(0, {"ok": True, "status": "PREFLIGHT_OK", "next_action": "proceed_to_render", "warnings": [], "errors": []}, "")), \
                    redirect_stdout(out):
                rc = cchouse.cmd_up(args)
            self.assertEqual(rc, 0, out.getvalue())
            env_text = (Path(td) / "priv" / "cc01" / "instance.env").read_text(encoding="utf-8")
            self.assertIn("SINGLE_TYPE=socks5", env_text)
            self.assertIn("SINGLE_VIA_TYPE=vless", env_text)
            self.assertIn("STRIPE_EXIT_MODE=front", env_text)
            for secret in ("demo-uuid-secret", "demo-pbk-secret", "exit_pass_secret"):
                self.assertNotIn(secret, out.getvalue())


class TestHostMihomoProbe(unittest.TestCase):
    def test_probe_config_minimal_strict_no_direct_no_tun_no_controller(self):
        exit_f, via_f = cchouse.parse_and_classify_links([LINK_VLESS, LINK_SOCKS])
        cfg = cchouse.build_host_probe_config(exit_f, 41234, via_fields=via_f)
        self.assertIn("mixed-port: 41234", cfg)
        self.assertIn("bind-address: 127.0.0.1", cfg)
        self.assertIn("allow-lan: false", cfg)
        self.assertIn("- MATCH,PROBE-Exit", cfg)
        self.assertIn("dialer-proxy: PROBE-Front", cfg)
        self.assertNotIn("DIRECT", cfg)
        self.assertNotIn("tun:", cfg)
        self.assertNotIn("external-controller", cfg)
        self.assertNotIn("dns:", cfg)

    def test_probe_temp_file_is_600_and_deleted_after_probe(self):
        exit_f, via_f = cchouse.parse_and_classify_links([LINK_VLESS, LINK_SOCKS])
        reply = {"ip": "198.51.100.88", "timezone": "America/Los_Angeles", "country": "US", "org": "AS64500 TestISP"}
        captured = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            cfg_path = Path(argv[argv.index("-f") + 1])
            captured["tmp_dir"] = cfg_path.parent
            captured["cfg_path"] = cfg_path
            if os.name != "nt":
                captured["dir_mode"] = stat.S_IMODE(cfg_path.parent.stat().st_mode)
                captured["cfg_mode"] = stat.S_IMODE(cfg_path.stat().st_mode)
            proc = mock.MagicMock()
            proc.poll.return_value = None
            proc.communicate.return_value = (b"", b"")
            return proc

        with mock.patch.object(cchouse.subprocess, "Popen", side_effect=fake_popen), \
                mock.patch.object(cchouse, "_wait_loopback_ready", return_value=True), \
                mock.patch.object(cchouse, "_curl_via_local_mihomo", return_value=(0, json.dumps(reply), "")), \
                mock.patch.object(cchouse.time, "sleep"):
            res = cchouse.probe_egress_via_proxy(exit_f, via_fields=via_f, probe_count=3, probe_interval=0, mihomo_bin="/fake/mihomo")

        self.assertEqual(res["ip"], "198.51.100.88")
        if os.name != "nt":
            self.assertEqual(captured["dir_mode"], 0o700)
            self.assertEqual(captured["cfg_mode"], 0o600)
        self.assertFalse(captured["tmp_dir"].exists(), "probe temp dir must be deleted after probe")
        self.assertFalse(captured["cfg_path"].exists())
        argv_str = " ".join(captured["argv"])
        for secret in ("demo-uuid-secret", "demo-pbk-secret", "exit_pass_secret", "exit_user"):
            self.assertNotIn(secret, argv_str)

    def test_probe_stops_on_inconsistent_ip_across_three_checks(self):
        exit_f = cchouse.parse_node_link(LINK_SOCKS)
        r1 = json.dumps({"ip": "198.51.100.1", "timezone": "America/Los_Angeles", "country": "US", "org": "AS64500 A"})
        r2 = json.dumps({"ip": "198.51.100.2", "timezone": "America/Los_Angeles", "country": "US", "org": "AS64500 A"})
        fake_proc = mock.MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.communicate.return_value = (b"", b"")
        tmp_dirs = []
        orig_mkdtemp = tempfile.mkdtemp

        def track_mkdtemp(**kw):
            d = orig_mkdtemp(**kw)
            tmp_dirs.append(Path(d))
            return d

        with mock.patch.object(cchouse.tempfile, "mkdtemp", side_effect=track_mkdtemp), \
                mock.patch.object(cchouse.subprocess, "Popen", return_value=fake_proc), \
                mock.patch.object(cchouse, "_wait_loopback_ready", return_value=True), \
                mock.patch.object(cchouse, "_curl_via_local_mihomo", side_effect=[(0, r1, ""), (0, r2, "")]), \
                mock.patch.object(cchouse.time, "sleep"):
            with self.assertRaises(cchouse.StepError) as cm:
                cchouse.probe_egress_via_proxy(exit_f, probe_count=3, probe_interval=0, mihomo_bin="/fake/mihomo")
        self.assertIn("不是固定 IP", str(cm.exception))
        self.assertNotIn("exit_pass_secret", str(cm.exception))
        self.assertTrue(tmp_dirs and not tmp_dirs[0].exists())

    def test_two_hop_failure_diagnosis_distinguishes_front_vs_exit_without_leaking(self):
        exit_f, via_f = cchouse.parse_and_classify_links([LINK_VLESS, LINK_SOCKS])
        fake_proc = mock.MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.communicate.return_value = (b"dial error: i/o timeout", b"")

        # 前置不通
        with mock.patch.object(cchouse.subprocess, "Popen", return_value=fake_proc), \
                mock.patch.object(cchouse, "_macos_default_route_is_utun", return_value=False), \
                mock.patch.object(cchouse, "_wait_loopback_ready", return_value=True), \
                mock.patch.object(cchouse, "_curl_via_local_mihomo", return_value=(28, "", "timeout")), \
                mock.patch.object(cchouse, "_probe_front_reachable", return_value=False):
            with self.assertRaises(cchouse.StepError) as cm:
                cchouse.probe_egress_via_proxy(exit_f, via_fields=via_f, mihomo_bin="/fake/mihomo")
        self.assertIn("前置节点", str(cm.exception))
        self.assertNotIn("demo-uuid-secret", str(cm.exception))
        self.assertNotIn("exit_pass_secret", str(cm.exception))

        # 前置正常，出口超时
        with mock.patch.object(cchouse.subprocess, "Popen", return_value=fake_proc), \
                mock.patch.object(cchouse, "_macos_default_route_is_utun", return_value=False), \
                mock.patch.object(cchouse, "_wait_loopback_ready", return_value=True), \
                mock.patch.object(cchouse, "_curl_via_local_mihomo", return_value=(28, "", "timeout")), \
                mock.patch.object(cchouse, "_probe_front_reachable", return_value=True):
            with self.assertRaises(cchouse.StepError) as cm:
                cchouse.probe_egress_via_proxy(exit_f, via_fields=via_f, mihomo_bin="/fake/mihomo")
        self.assertIn("前置节点正常，但出口节点超时", str(cm.exception))

        # 出口认证失败
        auth_proc = mock.MagicMock()
        auth_proc.poll.return_value = None
        auth_proc.communicate.return_value = (b"socks5 auth failed: username/password rejected", b"")
        with mock.patch.object(cchouse.subprocess, "Popen", return_value=auth_proc), \
                mock.patch.object(cchouse, "_macos_default_route_is_utun", return_value=False), \
                mock.patch.object(cchouse, "_wait_loopback_ready", return_value=True), \
                mock.patch.object(cchouse, "_curl_via_local_mihomo", return_value=(56, "", "proxy recv fail")), \
                mock.patch.object(cchouse, "_probe_front_reachable", return_value=True):
            with self.assertRaises(cchouse.StepError) as cm:
                cchouse.probe_egress_via_proxy(exit_f, via_fields=via_f, mihomo_bin="/fake/mihomo")
        self.assertIn("出口节点认证失败", str(cm.exception))

    def test_downloads_do_not_start_when_config_fails(self):
        with tempfile.TemporaryDirectory() as td:
            link_file = Path(td) / "bad.txt"
            link_file.write_text(LINK_VLESS + "\n", encoding="utf-8")
            if os.name != "nt":
                link_file.chmod(0o600)
            args = _up_args(td, link_file=str(link_file), config_only=False)
            out = io.StringIO()
            with mock.patch.object(cchouse, "_stdin_is_tty", return_value=False), \
                    mock.patch.object(cchouse, "probe_egress_via_proxy", side_effect=cchouse.StepError("前置节点连不上")), \
                    mock.patch.object(cchouse.UpContext, "start_downloads") as start_dl, \
                    redirect_stdout(out):
                rc = cchouse.cmd_up(args)
            self.assertNotEqual(rc, 0)
            start_dl.assert_not_called()

    def test_mihomo_manifest_loads_both_platforms(self):
        manifest = ROOT / "guest-linux" / "mihomo-manifest.json"
        for plat in ("darwin-arm64", "windows-amd64"):
            art = downloads.load_mihomo_artifact(manifest, platform_key=plat)
            self.assertEqual(art["version"], "v1.19.19")
            self.assertEqual(len(art["sha256"]), 64)


class TestTwoHopRenderVerifyAndDoctor(unittest.TestCase):
    def _two_hop_env(self):
        return {
            "GUEST_OS": "linux",
            "EXIT_MODE": "single",
            "TAG": "cc01",
            "VM_NAME": "claude-cc01",
            "GUEST_IF": "enp0s1",
            "MIHOMO_USER": "mihomo",
            "HOME_LAN_CIDR": "192.168.100.0/24",
            "VMNET_CIDR": "192.168.64.0/24",
            "GUEST_IP": "192.168.64.2",
            "ROUTER_IP": "192.168.100.1",
            "TIMEZONE": "America/Los_Angeles",
            "EXPECTED_EGRESS_IP": "198.51.100.88",
            "EXPECTED_EGRESS_LOC": "US",
            "EXPECTED_EGRESS_ASN": "AS64500",
            "SSH_PUBKEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey test",
            "SINGLE_TYPE": "socks5",
            "SINGLE_SERVER": "198.51.100.20",
            "SINGLE_SERVER_IP": "198.51.100.20",
            "SINGLE_PORT": "8000",
            "SINGLE_USER": "exit_user",
            "SINGLE_PASS": "exit_pass_secret",
            "SINGLE_VIA_TYPE": "vless",
            "SINGLE_VIA_SERVER": "front.example.com",
            "SINGLE_VIA_SERVER_IP": "203.0.113.10",
            "SINGLE_VIA_PORT": "443",
            "SINGLE_VIA_UUID": "demo-uuid-secret",
            "SINGLE_VIA_SNI": "sni.example.com",
            "SINGLE_VIA_FINGERPRINT": "chrome",
            "SINGLE_VIA_FLOW": "xtls-rprx-vision",
            "SINGLE_VIA_NETWORK": "tcp",
            "SINGLE_VIA_PUBLIC_KEY": "demo-pbk-secret",
            "SINGLE_VIA_SHORT_ID": "abcd1234",
        }

    def test_render_two_hop_dialer_proxy_front_only_nftables_and_stripe_front(self):
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / "instance.env"
            out_dir = Path(td) / "rendered"
            env_data = self._two_hop_env()
            env_path.write_text("\n".join(f"{k}='{v}'" for k, v in env_data.items()) + "\n", encoding="utf-8")

            # preflight passes and defaults stripe_mode to front
            errs, warns = preflight_mod.validate_env_file(str(env_path), "tart-linux")
            self.assertEqual(errs, [], warns)

            env_loaded = render_mod.load_env(env_path)
            stripe_mode = render_mod.effective_stripe_mode(env_loaded, "linux", "single")
            self.assertEqual(stripe_mode, "front")

            render_mod.render_linux(env_loaded, out_dir, "single", stripe_mode, allow_resolve=False)
            cfg = (out_dir / "config.yaml").read_text(encoding="utf-8")
            nft = (out_dir / "nftables.conf").read_text(encoding="utf-8")

            self.assertIn("- name: CC01-Single-Front", cfg)
            self.assertIn("server: 203.0.113.10", cfg)
            self.assertIn("servername: sni.example.com", cfg)
            self.assertIn("- name: CC01-Single-Exit", cfg)
            self.assertIn("dialer-proxy: CC01-Single-Front", cfg)
            self.assertIn("- DOMAIN-SUFFIX,stripe.com,CC01-Stripe", cfg)
            self.assertIn("- name: CC01-Stripe\n  type: select\n  proxies:\n  - CC01-Single-Front", cfg)
            self.assertIn("- NETWORK,UDP,REJECT", cfg)
            self.assertNotIn("DIRECT", cfg)

            # nftables 白名单只放行前置 IP:端口，绝不放行出口 IP:端口
            self.assertIn('meta skuid "mihomo" ip daddr 203.0.113.10 tcp dport 443 accept', nft)
            self.assertNotIn("198.51.100.20", nft)
            self.assertNotIn("dport 8000", nft)

    def test_verify_native_exports_expected_nft_egress_sha_without_leaking_ips(self):
        with tempfile.TemporaryDirectory() as td:
            identity = Path(td) / "id_ed25519"
            known_hosts = Path(td) / "known_hosts"
            env = dict(self._two_hop_env(), SSH_KEY=str(identity), SSH_KNOWN_HOSTS=str(known_hosts), GUEST_IP="127.0.0.1", SSH_PORT="2222")
            with mock.patch.object(verify_native.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                rc = verify_native.verify(env, ["--fail-closed"])
            self.assertEqual(rc, 0)
            if os.name == "nt":
                args = run.call_args.args[0]
                b64 = args[-1].split()[2]
                payload = gzip.decompress(base64.b64decode(b64)).decode("utf-8")
            else:
                payload = run.call_args.kwargs["input"].decode("utf-8")
            expected_sha = hashlib.sha256(b"203.0.113.10:443\n").hexdigest()
            self.assertIn(f"export CCHOUSE_EXPECTED_NFT_EGRESS_SHA={expected_sha}\n", payload)
            # 前置和出口 IP/凭据都不进入 payload
            for secret in ("203.0.113.10", "198.51.100.20", "demo-uuid-secret", "exit_pass_secret"):
                self.assertNotIn(secret, payload)

    def test_doctor_redacts_single_via_secrets(self):
        env = self._two_hop_env()
        secrets = doctor_mod.extract_sensitive_values(env)
        for s in ("203.0.113.10", "demo-uuid-secret", "demo-pbk-secret", "exit_pass_secret", "front.example.com"):
            self.assertIn(s, secrets)
        self.assertNotIn("vless", secrets)
        self.assertNotIn("chrome", secrets)


if __name__ == "__main__":
    unittest.main()
