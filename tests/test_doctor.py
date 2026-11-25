import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import doctor as doctor_mod  # noqa: E402


def _fake_uuid(a, b, c, d, e):
    return "-".join((a, b, c, d, e))


SINGLE_TEST_UUID = _fake_uuid("11111111", "2222", "3333", "4444", "555555555555")
STRIPE_TEST_UUID = _fake_uuid("66666666", "7777", "8888", "9999", "aaaaaaaaaaaa")
FRONT_TEST_UUID = _fake_uuid("bbbbbbbb", "cccc", "dddd", "eeee", "ffffffffffff")
NODE_TEST_UUID = _fake_uuid("12345678", "1234", "5678", "9abc", "def012345678")

FULL_SENSITIVE_ENV = f"""\
GUEST_OS=linux
EXIT_MODE=single
STRIPE_EXIT_MODE=node
TAG=doc01
VM_NAME=claude-doc01
GUEST_IF=ens33
MIHOMO_USER=mihomo
VMNET_CIDR=192.168.180.0/24
GUEST_IP=192.168.180.128
ROUTER_IP=192.168.100.1
HOME_LAN_CIDR=192.168.100.0/24
TIMEZONE=America/Los_Angeles
GUEST_LOCALE=en_US.UTF-8
GUEST_KEYBOARD=us
EXPECTED_EGRESS_IP=198.51.100.77
EXPECTED_EGRESS_LOC=US
EXPECTED_EGRESS_ASN=AS64512
SSH_KEY='/tmp/private-doc01/id_ed25519_secret_path'
SSH_KNOWN_HOSTS='/tmp/private-doc01/known_hosts'
SSH_PUBKEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockPubKeySecretMaterial9876543210 claude@doc01'

SINGLE_TYPE=socks5
SINGLE_SERVER='single-node.secret-isp.example'
SINGLE_SERVER_IP='203.0.113.41'
SINGLE_PORT='1080'
SINGLE_USER='single_user_alpha'
SINGLE_PASS='S!ngleP@ss#9988'
SINGLE_UUID='{SINGLE_TEST_UUID}'
SINGLE_SNI='sni.single.example'
SINGLE_PUBLIC_KEY='SingleRealityPubKey_XYZ123456'
SINGLE_SHORT_ID='a1b2c3d4'
SINGLE_WS_PATH='/ws-secret-single-path'
SINGLE_WS_HOST='ws.single.secret.example'
SINGLE_HOST_KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISingleHostKeyMaterial1234567890'

STRIPE_NODE_TYPE=vless
STRIPE_NODE_SERVER='stripe-node.secret-vps.example'
STRIPE_NODE_SERVER_IP='203.0.113.42'
STRIPE_NODE_PORT='443'
STRIPE_NODE_USER='stripe_user_beta'
STRIPE_NODE_PASS='Str!peP@ss#7766'
STRIPE_NODE_UUID='{STRIPE_TEST_UUID}'
STRIPE_NODE_SNI='sni.stripe.example'
STRIPE_NODE_PUBLIC_KEY='StripeRealityPubKey_ABC987654'
STRIPE_NODE_SHORT_ID='f0e1d2c3'
STRIPE_NODE_WS_PATH='/ws-secret-stripe-path'
STRIPE_NODE_WS_HOST='ws.stripe.secret.example'
STRIPE_NODE_HOST_KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIStripeHostKeyMaterial0987654321'

WEBSHARE_HOST='203.0.113.43'
WEBSHARE_PORT='8000'
WEBSHARE_USER='webshare_user_gamma'
WEBSHARE_PASS='W3bsh@reP@ss#5544'

FRONT_SERVER_IP='203.0.113.44'
FRONT_PORT='443'
FRONT_UUID='{FRONT_TEST_UUID}'
FRONT_SNI='front.sni.example'
FRONT_PUBLIC_KEY='FrontRealityPubKey_QWE456789'
FRONT_SHORT_ID='99887766'
FRONT_CF_SERVER_IP='203.0.113.45'
FRONT_CF_HOST='cf.front.secret.example'
FRONT_CF_WS_PATH='/cf-secret-ws-path'

NODE_LABEL=SJC01
NODE_UUID='{NODE_TEST_UUID}'
NODE_WS_SERVER_IP='203.0.113.46'
NODE_WS_HOST='node.ws.secret.example'
NODE_WS_PATH='/node-secret-ws-path'
NODE_REALITY_SERVER_IP='203.0.113.47'
NODE_REALITY_SNI='node.reality.sni.example'
NODE_REALITY_PUBLIC_KEY='NodeRealityPubKey_RTY321654'
NODE_REALITY_SHORT_ID='55443322'
"""


class TestDoctorBundle(unittest.TestCase):
    def test_redaction_strips_all_env_secrets_from_every_zip_entry(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            env_file = td_path / "instance.env"
            env_file.write_text(FULL_SENSITIVE_ENV, encoding="utf-8")
            out_zip = td_path / "doctor.zip"

            parsed_env = doctor_mod.render_mod.load_env(env_file)
            sensitive_values = doctor_mod.extract_sensitive_values(parsed_env)
            self.assertGreater(len(sensitive_values), 25)

            pem_body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCtestSecretKeyBody"
            pem_header = "-----" + "BEGIN " + "OPENSSH PRIVATE KEY-----"
            pem_footer = "-----" + "END " + "OPENSSH PRIVATE KEY-----"
            pem_block = f"{pem_header}\n{pem_body}\n{pem_footer}"
            inline_url_pass = "InlineUrlSecretPass999"
            inline_url_user = "InlineUrlUser888"
            inline_bearer = "ghp_SecretBearerToken1234567890abcdef"
            inline_uuid = _fake_uuid("deadbeef", "cafe", "4bad", "8f00", "123456789abc")
            basic_b64 = base64.b64encode(b"single_user_alpha:S!ngleP@ss#9988").decode("ascii")
            quoted_pass = urllib.parse.quote("S!ngleP@ss#9988", safe="")

            # 把 instance.env 里的全部敏感值与多种常见凭据格式塞进每一类输出
            all_secrets_blob = "\n".join(f"leak_{i}={v}" for i, v in enumerate(sensitive_values))
            fake_preflight = {
                "exit_code": 0,
                "error": None,
                "json": {
                    "ok": True,
                    "status": "PREFLIGHT_OK",
                    "warnings": [
                        f"debug proxy=socks5://{inline_url_user}:{inline_url_pass}@203.0.113.41:1080",
                        f"Authorization: Bearer {inline_bearer}",
                        f"Proxy-Authorization: Basic {basic_b64}",
                        f"encoded={quoted_pass}",
                        all_secrets_blob,
                    ],
                },
                "raw_stdout": None,
                "stderr": f"vless://{inline_uuid}@203.0.113.42:443",
            }
            fake_host_info = {
                "os": "Linux",
                "arch": "amd64",
                "tart": {"found": False, "version": None},
                "vmrun": {"found": True, "version": "vmrun version 1.17.0"},
                "debug_leak": f"{pem_block}\n{all_secrets_blob}",
            }
            fake_firstboot = f"[firstboot] started\n{all_secrets_blob}\n{pem_block}\n"
            fake_mihomo = (
                "=== is-active ===\nactive\n"
                "=== status ===\n● cchouse-mihomo.service - mihomo\n"
                f"dial socks5://{inline_url_user}:{inline_url_pass}@203.0.113.41:1080\n"
                f"pass" + f"word: '{parsed_env['WEBSHARE_PASS']}'\n"
                f"{all_secrets_blob}\n"
            )
            fake_nft = (
                "=== cchouse-probe nft-loaded ===\nok\n"
                "=== cchouse-probe nft-live-sha ===\nabcdef123456\n"
                "=== nft list ruleset ===\n"
                "table inet cchouse_guard {\n"
                f"  ip daddr {{ {parsed_env['SINGLE_SERVER_IP']}, {parsed_env['STRIPE_NODE_SERVER_IP']} }} accept\n"
                f"  # {all_secrets_blob}\n"
                "}\n__NFT_OK__\n"
            )
            fake_verify_stdout = (
                '{"ok":true,"pass":18,"fail":0,"warn":0,"stripe_reachable":true,'
                f'"next_action":"human_check_html_and_login","debug":"{parsed_env["SINGLE_PASS"]}"}}\n'
            )

            def fake_ssh(_env, remote_cmd, timeout=10):
                if remote_cmd == "true":
                    return 0, "", "", None
                if "firstboot.log" in remote_cmd:
                    return 0, fake_firstboot, "", None
                if "cchouse-mihomo.service" in remote_cmd:
                    return 0, fake_mihomo, "", None
                if "nft list ruleset" in remote_cmd:
                    return 0, fake_nft, "", None
                return 1, "", "unexpected cmd", None

            with (
                mock.patch.object(doctor_mod, "run_preflight", return_value=fake_preflight),
                mock.patch.object(doctor_mod, "collect_host_info", return_value=fake_host_info),
                mock.patch.object(doctor_mod, "run_ssh_command", side_effect=fake_ssh),
                mock.patch.object(
                    doctor_mod,
                    "run_verify_linux",
                    return_value=(0, fake_verify_stdout, f"stderr leak {parsed_env['FRONT_UUID']}", None),
                ),
            ):
                summary = doctor_mod.build_doctor_bundle(
                    env_path=str(env_file),
                    vm_ssh=True,
                    out_path=str(out_zip),
                )

            self.assertTrue(summary["ok"])
            self.assertTrue(summary["vm_ssh_reachable"])
            self.assertTrue(out_zip.is_file())

            forbidden = set(sensitive_values) | {
                pem_body,
                inline_url_pass,
                inline_url_user,
                inline_bearer,
                inline_uuid,
                basic_b64,
                quoted_pass,
            }

            with zipfile.ZipFile(out_zip, "r") as zf:
                names = set(zf.namelist())
                for expected in (
                    "manifest.json",
                    "host-info.json",
                    "preflight.json",
                    "env-summary.json",
                    "guest/firstboot.log",
                    "guest/verify-linux.json",
                    "guest/mihomo-status.txt",
                    "guest/nft-ruleset.txt",
                ):
                    self.assertIn(expected, names)

                for name in names:
                    content = zf.read(name).decode("utf-8")
                    for secret in forbidden:
                        self.assertNotIn(
                            secret,
                            content,
                            f"敏感值泄漏到 zip 内文件 {name}",
                        )

                # 确认非敏感结构字段未被误杀且 JSON 仍合法
                verify_data = json.loads(zf.read("guest/verify-linux.json").decode("utf-8"))
                self.assertEqual(verify_data["result"]["pass"], 18)
                self.assertEqual(verify_data["result"]["fail"], 0)
                mihomo_txt = zf.read("guest/mihomo-status.txt").decode("utf-8")
                self.assertIn("cchouse-mihomo.service", mihomo_txt)

    def test_unreachable_guest_still_produces_zip_and_marks_missing(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            env_file = td_path / "instance.env"
            env_file.write_text(FULL_SENSITIVE_ENV, encoding="utf-8")
            out_zip = td_path / "doctor-offline.zip"

            with (
                mock.patch.object(
                    doctor_mod,
                    "run_preflight",
                    return_value={"exit_code": 0, "error": None, "json": {"ok": True}, "raw_stdout": None, "stderr": None},
                ),
                mock.patch.object(
                    doctor_mod,
                    "run_ssh_command",
                    return_value=(255, "", "ssh: connect to host 192.168.180.128 port 22: Connection timed out", None),
                ),
            ):
                summary = doctor_mod.build_doctor_bundle(
                    env_path=str(env_file),
                    vm_ssh=True,
                    out_path=str(out_zip),
                )

            self.assertTrue(summary["ok"])
            self.assertFalse(summary["vm_ssh_reachable"])
            self.assertTrue(out_zip.is_file())

            with zipfile.ZipFile(out_zip, "r") as zf:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                self.assertTrue(manifest["vm_ssh_requested"])
                self.assertFalse(manifest["vm_ssh_reachable"])
                missing_map = {m["item"]: m["reason"] for m in manifest["missing"]}
                for item in (
                    "guest/firstboot.log",
                    "guest/verify-linux.json",
                    "guest/mihomo-status.txt",
                    "guest/nft-ruleset.txt",
                ):
                    self.assertIn(item, missing_map)
                    self.assertIn("ssh_unreachable", missing_map[item])

    def test_cli_end_to_end_cross_platform(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            env_file = td_path / "instance.env"
            env_file.write_text(FULL_SENSITIVE_ENV, encoding="utf-8")
            out_zip = td_path / "cli-doctor.zip"

            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "scripts" / "doctor.py"),
                    "--env",
                    str(env_file),
                    "--out",
                    str(out_zip),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
            summary = json.loads(proc.stdout)
            self.assertTrue(summary["ok"])
            self.assertTrue(out_zip.is_file())

            with zipfile.ZipFile(out_zip, "r") as zf:
                names = set(zf.namelist())
                self.assertIn("manifest.json", names)
                self.assertIn("host-info.json", names)
                self.assertIn("preflight.json", names)
                self.assertIn("env-summary.json", names)
                host_info = json.loads(zf.read("host-info.json").decode("utf-8"))
                self.assertIn("os", host_info)
                self.assertIn("arch", host_info)
                self.assertIn("tart", host_info)
                self.assertIn("vmrun", host_info)
                for name in names:
                    raw = zf.read(name).decode("utf-8")
                    self.assertNotIn("S!ngleP@ss#9988", raw)
                    self.assertNotIn("W3bsh@reP@ss#5544", raw)
                    self.assertNotIn(SINGLE_TEST_UUID, raw)

    def test_cli_without_env_still_produces_zip(self):
        with tempfile.TemporaryDirectory() as td:
            out_zip = Path(td) / "no-env.zip"
            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "scripts" / "doctor.py"),
                    "--out",
                    str(out_zip),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
            self.assertTrue(out_zip.is_file())
            with zipfile.ZipFile(out_zip, "r") as zf:
                names = set(zf.namelist())
                self.assertIn("manifest.json", names)
                self.assertIn("host-info.json", names)
                self.assertIn("preflight.json", names)

    def test_redacts_host_home_paths_and_username(self):
        import getpass

        real_user = getpass.getuser()
        home_str = str(Path.home())
        with tempfile.TemporaryDirectory() as td:
            out_zip = Path(td) / "home-redact.zip"
            fake_preflight = {
                "exit_code": 0,
                "error": None,
                "json": {
                    "ok": True,
                    "status": "PREFLIGHT_OK",
                    "host": {
                        "tart_home": f"{home_str}/Library/Application Support/cchouse/tart",
                    },
                    "warnings": [
                        f"mac path: /Users/alice_secret/Library/Application Support/cchouse",
                        r"win path: C:\Users\bob_secret\AppData\Local\cchouse",
                        "win slash path: C:/Users/dave_secret/AppData/Local/cchouse",
                        "linux path: /home/carol_secret/.cchouse/win01",
                        f"current user mention: owner={real_user}",
                    ],
                },
                "raw_stdout": None,
                "stderr": None,
            }
            with mock.patch.object(doctor_mod, "run_preflight", return_value=fake_preflight):
                summary = doctor_mod.build_doctor_bundle(env_path=None, vm_ssh=False, out_path=str(out_zip))

            self.assertTrue(summary["ok"])
            with zipfile.ZipFile(out_zip, "r") as zf:
                for name in zf.namelist():
                    content = zf.read(name).decode("utf-8")
                    if len(real_user) >= 3:
                        self.assertNotIn(real_user, content, f"真实用户名泄漏到 {name}")
                    for fake_u in ("alice_secret", "bob_secret", "carol_secret", "dave_secret"):
                        self.assertNotIn(fake_u, content, f"路径用户名 {fake_u} 泄漏到 {name}")
                preflight_txt = zf.read("preflight.json").decode("utf-8")
                self.assertIn("~/", preflight_txt)
                self.assertIn("/Users/<user>/", preflight_txt)
                self.assertIn("/home/<user>/", preflight_txt)
                self.assertIn("<user>", preflight_txt)


if __name__ == "__main__":
    unittest.main()
