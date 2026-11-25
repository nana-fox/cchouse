import base64
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import cchouse as cchouse_mod  # noqa: E402
import render as render_mod  # noqa: E402


def _fake_uuid(a, b, c, d, e):
    return "-".join((a, b, c, d, e))


class TestNodeLinkParser(unittest.TestCase):
    def test_parse_socks5_link_with_encoded_creds(self):
        res = cchouse_mod.parse_node_link("socks5://user%40isp:p%40ss%23123@203.0.113.10:1080#us-isp")
        self.assertEqual(res["SINGLE_TYPE"], "socks5")
        self.assertEqual(res["SINGLE_SERVER"], "203.0.113.10")
        self.assertEqual(res["SINGLE_SERVER_IP"], "203.0.113.10")
        self.assertEqual(res["SINGLE_PORT"], "1080")
        self.assertEqual(res["SINGLE_USER"], "user@isp")
        self.assertEqual(res["SINGLE_PASS"], "p@ss#123")
        self.assertEqual(res["SINGLE_TLS"], "")

    def test_parse_http_and_https_links(self):
        http_res = cchouse_mod.parse_node_link("http://u1:p1@proxy.example.com:8080")
        self.assertEqual(http_res["SINGLE_TYPE"], "http")
        self.assertEqual(http_res["SINGLE_SERVER"], "proxy.example.com")
        self.assertEqual(http_res["SINGLE_SERVER_IP"], "")
        self.assertEqual(http_res["SINGLE_PORT"], "8080")
        self.assertEqual(http_res["SINGLE_USER"], "u1")
        self.assertEqual(http_res["SINGLE_PASS"], "p1")
        self.assertEqual(http_res["SINGLE_TLS"], "")

        https_res = cchouse_mod.parse_node_link("https://u2:p2@proxy.example.com")
        self.assertEqual(https_res["SINGLE_TYPE"], "http")
        self.assertEqual(https_res["SINGLE_PORT"], "443")
        self.assertEqual(https_res["SINGLE_TLS"], "true")

    def test_parse_vless_reality_and_ws(self):
        uid = _fake_uuid("11111111", "2222", "3333", "4444", "555555555555")
        reality_link = (
            f"vless://{uid}@203.0.113.20:443"
            "?security=reality&sni=www.example.com&fp=chrome&pbk=TestPubKey123456&sid=a1b2c3d4"
            "&flow=xtls-rprx-vision&type=tcp#node1"
        )
        res = cchouse_mod.parse_node_link(reality_link)
        self.assertEqual(res["SINGLE_TYPE"], "vless")
        self.assertEqual(res["SINGLE_UUID"], uid)
        self.assertEqual(res["SINGLE_SERVER"], "203.0.113.20")
        self.assertEqual(res["SINGLE_PORT"], "443")
        self.assertEqual(res["SINGLE_SNI"], "www.example.com")
        self.assertEqual(res["SINGLE_PUBLIC_KEY"], "TestPubKey123456")
        self.assertEqual(res["SINGLE_SHORT_ID"], "a1b2c3d4")
        self.assertEqual(res["SINGLE_FLOW"], "xtls-rprx-vision")
        self.assertEqual(res["SINGLE_NETWORK"], "tcp")

        ws_link = (
            f"vless://{uid}@vless.example.com:443"
            "?security=tls&type=ws&path=%2Fws-secret&host=cdn.example.com&sni=cdn.example.com"
        )
        ws_res = cchouse_mod.parse_node_link(ws_link)
        self.assertEqual(ws_res["SINGLE_NETWORK"], "ws")
        self.assertEqual(ws_res["SINGLE_WS_PATH"], "/ws-secret")
        self.assertEqual(ws_res["SINGLE_WS_HOST"], "cdn.example.com")

    def test_parse_trojan_link(self):
        res = cchouse_mod.parse_node_link(
            "trojan://Tr0jan%21Pass@trojan.example.com:443?sni=trojan.example.com&type=ws&path=%2Ftr&host=trojan.example.com#tr"
        )
        self.assertEqual(res["SINGLE_TYPE"], "trojan")
        self.assertEqual(res["SINGLE_PASS"], "Tr0jan!Pass")
        self.assertEqual(res["SINGLE_SERVER"], "trojan.example.com")
        self.assertEqual(res["SINGLE_PORT"], "443")
        self.assertEqual(res["SINGLE_NETWORK"], "ws")
        self.assertEqual(res["SINGLE_WS_PATH"], "/tr")

    def test_parse_ss_sip002_and_legacy(self):
        # SIP002
        userinfo = base64.urlsafe_b64encode(b"aes-256-gcm:SS_Secret_Pass_1").decode("ascii").rstrip("=")
        res = cchouse_mod.parse_node_link(f"ss://{userinfo}@203.0.113.30:8388#ss1")
        self.assertEqual(res["SINGLE_TYPE"], "ss")
        self.assertEqual(res["SINGLE_CIPHER"], "aes-256-gcm")
        self.assertEqual(res["SINGLE_PASS"], "SS_Secret_Pass_1")
        self.assertEqual(res["SINGLE_SERVER"], "203.0.113.30")
        self.assertEqual(res["SINGLE_PORT"], "8388")

        # Legacy
        legacy_payload = base64.b64encode(b"chacha20-ietf-poly1305:LegacyPass2@ss.example.com:9000").decode("ascii")
        res2 = cchouse_mod.parse_node_link(f"ss://{legacy_payload}#ss2")
        self.assertEqual(res2["SINGLE_TYPE"], "ss")
        self.assertEqual(res2["SINGLE_CIPHER"], "chacha20-ietf-poly1305")
        self.assertEqual(res2["SINGLE_PASS"], "LegacyPass2")
        self.assertEqual(res2["SINGLE_SERVER"], "ss.example.com")
        self.assertEqual(res2["SINGLE_PORT"], "9000")

    def test_rejects_udp_protocols_without_echoing_secret(self):
        secret = "SuperSecretUdpPass999"
        for proto in ("hysteria2", "hy2", "hysteria", "tuic", "wireguard", "wg"):
            link = f"{proto}://{secret}@203.0.113.99:443?sni=example.com"
            with self.assertRaises(cchouse_mod.NodeLinkError) as ctx:
                cchouse_mod.parse_node_link(link)
            msg = str(ctx.exception)
            self.assertIn("UDP", msg)
            self.assertNotIn(secret, msg)

    def test_rejects_malicious_inputs_and_never_leaks_creds(self):
        bad_secret = "EvilInjectedPass888"
        bad_links = [
            "",
            "   ",
            "not-a-url",
            f"socks5://u:{bad_secret}'@203.0.113.10:1080",  # single-quote injection
            f"socks5://u:{bad_secret}\nEXIT_MODE=chain@203.0.113.10:1080",  # newline injection
            f"socks5://u:{bad_secret}@203.0.113.10:99999",  # invalid port
            f"socks5://u:{bad_secret}@203.0.113.10:abc",  # non-numeric port
            f"socks5://u:{bad_secret}@[2001:db8::1]:1080",  # IPv6 literal
            f"socks5://u:{bad_secret}@bad;rm -rf /:1080",  # shell metachar in host
            f"vless://{_fake_uuid('11111111', '2222', '3333', '4444', '555555555555')}@203.0.113.20:443?security=none",  # plaintext vless
            f"vless://{_fake_uuid('11111111', '2222', '3333', '4444', '555555555555')}@203.0.113.20:443?security=reality",  # missing pbk
            f"vless://{_fake_uuid('11111111', '2222', '3333', '4444', '555555555555')}@203.0.113.20:443?security=tls&type=grpc",  # unsupported net
            "trojan://@203.0.113.20:443",  # empty password
            f"trojan://{bad_secret}@203.0.113.20:443?security=none",  # plaintext trojan
            "ss://!!!not-base64!!!@203.0.113.30:8388",  # invalid base64
            "ftp://user:pass@203.0.113.10:21",  # unsupported scheme
        ]
        for bad in bad_links:
            with self.assertRaises(cchouse_mod.NodeLinkError) as ctx:
                cchouse_mod.parse_node_link(bad)
            self.assertNotIn(bad_secret, str(ctx.exception))


class TestCchouseUpCLI(unittest.TestCase):
    def test_up_generates_secure_env_without_echoing_link_or_password(self):
        secret_pass = "UltraSecretUpPass!7744"
        secret_user = "up_secret_user"
        link = f"socks5://{secret_user}:{secret_pass}@203.0.113.55:1080#my-isp\n"

        with tempfile.TemporaryDirectory() as td:
            priv_dir = Path(td) / "cc09"
            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "scripts" / "cchouse.py"),
                    "up",
                    "--tag",
                    "cc09",
                    "--private-dir",
                    str(priv_dir),
                    "--timezone",
                    "America/Los_Angeles",
                    "--expected-ip",
                    "203.0.113.55",
                    "--link-stdin",
                    "--non-interactive",
                    "--no-probe",
                    "--config-only",
                    "--cache-dir",
                    str(Path(td) / "cache"),
                ],
                input=link,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
                timeout=30,
            )

            combined_out = proc.stdout + "\n" + proc.stderr
            # 红线：链接与凭据绝不回显到终端输出
            self.assertNotIn(secret_pass, combined_out)
            self.assertNotIn(secret_user, combined_out)
            self.assertNotIn("socks5://", combined_out)

            env_file = priv_dir / "instance.env"
            self.assertTrue(env_file.is_file(), f"instance.env 未生成，stderr={proc.stderr}")
            self.assertTrue((priv_dir / "id_ed25519").is_file())
            self.assertTrue((priv_dir / "id_ed25519.pub").is_file())

            if os.name != "nt":
                mode = stat.S_IMODE(os.stat(env_file).st_mode)
                self.assertEqual(mode, 0o600)

            loaded = render_mod.load_env(env_file)
            self.assertEqual(loaded["GUEST_OS"], "linux")
            self.assertEqual(loaded["EXIT_MODE"], "single")
            self.assertEqual(loaded["STRIPE_EXIT_MODE"], "off")
            self.assertEqual(loaded["SINGLE_TYPE"], "socks5")
            self.assertEqual(loaded["SINGLE_SERVER"], "203.0.113.55")
            self.assertEqual(loaded["SINGLE_SERVER_IP"], "203.0.113.55")
            self.assertEqual(loaded["SINGLE_PORT"], "1080")
            self.assertEqual(loaded["SINGLE_USER"], secret_user)
            self.assertEqual(loaded["SINGLE_PASS"], secret_pass)
            self.assertTrue(loaded["SSH_PUBKEY"].startswith("ssh-ed25519 "))

    def test_up_refuses_private_dir_inside_repo(self):
        inside_dir = ROOT / "tmp-should-refuse"
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                str(ROOT / "scripts" / "cchouse.py"),
                "up",
                "--private-dir",
                str(inside_dir),
                "--link-stdin",
                "--non-interactive",
                "--no-probe",
            ],
            input="socks5://u:p@203.0.113.55:1080\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(inside_dir.exists())


if __name__ == "__main__":
    unittest.main()
