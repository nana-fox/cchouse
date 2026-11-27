import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import cchouse  # noqa: E402

VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()


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
        dry_run=False,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


class TestReadLinkFile(unittest.TestCase):
    def test_missing_file_raises_without_leaking(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "nope.txt"
            with self.assertRaisesRegex(cchouse.NodeLinkError, "找不到链接文件"):
                cchouse.read_link_file(str(missing))

    def test_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(cchouse.NodeLinkError, "是目录"):
                cchouse.read_link_file(td)

    @unittest.skipIf(os.name == "nt", "POSIX symlink check")
    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "real.txt"
            target.write_text("socks5://user:pass@203.0.113.5:1080\n", encoding="utf-8")
            target.chmod(0o600)
            link = Path(td) / "sym.txt"
            link.symlink_to(target)
            with self.assertRaisesRegex(cchouse.NodeLinkError, "普通文件"):
                cchouse.read_link_file(str(link))

    def test_wrong_owner_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "node.txt"
            p.write_text("socks5://user:secretpass@203.0.113.5:1080\n", encoding="utf-8")
            if os.name != "nt":
                p.chmod(0o600)
            with mock.patch.object(cchouse, "_is_owned_by_current_user", return_value=False):
                with self.assertRaises(cchouse.NodeLinkError) as cm:
                    cchouse.read_link_file(str(p))
            self.assertIn("不属于当前用户", str(cm.exception))
            self.assertNotIn("secretpass", str(cm.exception))

    def test_empty_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "empty.txt"
            p.write_text("   \n\n\t\n", encoding="utf-8")
            if os.name != "nt":
                p.chmod(0o600)
            with self.assertRaisesRegex(cchouse.NodeLinkError, "链接文件为空"):
                cchouse.read_link_file(str(p))

    def test_reads_first_non_empty_line_and_strips_bom(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "node.txt"
            p.write_bytes("\ufeff\n  \n  socks5://u:p@203.0.113.5:1080  \nsecond-line\n".encode("utf-8"))
            if os.name != "nt":
                p.chmod(0o600)
            self.assertEqual(cchouse.read_link_file(str(p)), "socks5://u:p@203.0.113.5:1080")

    @unittest.skipIf(os.name == "nt", "POSIX permission warning")
    def test_group_or_other_readable_warns_and_continues(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "wide.txt"
            p.write_text("socks5://u:secret99@203.0.113.5:1080\n", encoding="utf-8")
            p.chmod(0o644)
            err = io.StringIO()
            with redirect_stderr(err):
                got = cchouse.read_link_file(str(p))
            self.assertEqual(got, "socks5://u:secret99@203.0.113.5:1080")
            self.assertIn("警告", err.getvalue())
            self.assertIn("chmod 600", err.getvalue())
            self.assertNotIn("secret99", err.getvalue())


class TestUpLinkFileAndRebuildPrompt(unittest.TestCase):
    def test_up_with_link_file_works_without_tty_and_keeps_file_and_no_leak(self):
        secret_host = "203.0.113.88"
        secret_user = "bob_secret"
        secret_pass = "top_secret_pw"
        with tempfile.TemporaryDirectory() as td:
            link_file = Path(td) / "my-node.txt"
            link_file.write_text(f"\nsocks5://{secret_user}:{secret_pass}@{secret_host}:1080\n", encoding="utf-8")
            if os.name != "nt":
                link_file.chmod(0o600)
            args = _up_args(td, link_file=str(link_file), config_only=True)
            egress = {"ip": "198.51.100.9", "country": "US", "asn": "AS64500", "timezone": "America/Los_Angeles"}
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cchouse, "_stdin_is_tty", return_value=False), \
                    mock.patch.object(cchouse, "probe_egress_via_proxy", return_value=egress), \
                    mock.patch.object(cchouse, "run_preflight_check", return_value=(0, {"ok": True, "status": "PREFLIGHT_OK", "next_action": "proceed_to_render", "warnings": [], "errors": []}, "")), \
                    redirect_stdout(out), redirect_stderr(err):
                rc = cchouse.cmd_up(args)
            self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
            self.assertTrue(link_file.is_file(), "cchouse must not auto-delete user link file")
            combined = out.getvalue() + err.getvalue()
            self.assertIn(f"可以删掉 {link_file} 了", combined)
            priv = Path(td) / "priv" / "cc01"
            state_text = (priv / "state.json").read_text(encoding="utf-8") if (priv / "state.json").is_file() else ""
            for secret in (secret_host, secret_user, secret_pass):
                self.assertNotIn(secret, combined)
                self.assertNotIn(secret, state_text)
            env_text = (priv / "instance.env").read_text(encoding="utf-8")
            self.assertIn(f"SINGLE_SERVER='{secret_host}'", env_text)

    def test_up_link_file_unsupported_protocol_rejected_without_leak(self):
        with tempfile.TemporaryDirectory() as td:
            link_file = Path(td) / "hy2.txt"
            link_file.write_text("hysteria2://secret-token-xyz@example.invalid:443?sni=example.invalid\n", encoding="utf-8")
            if os.name != "nt":
                link_file.chmod(0o600)
            args = _up_args(td, link_file=str(link_file), config_only=True)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cchouse, "_stdin_is_tty", return_value=False), \
                    redirect_stdout(out), redirect_stderr(err):
                rc = cchouse.cmd_up(args)
            self.assertNotEqual(rc, 0)
            combined = out.getvalue() + err.getvalue()
            self.assertIn("不支持 UDP 协议", combined)
            self.assertNotIn("example.invalid", combined)
            self.assertNotIn("secret-token-xyz", combined)

    def test_up_link_file_invalid_format_fails_without_leak(self):
        with tempfile.TemporaryDirectory() as td:
            link_file = Path(td) / "bad.txt"
            link_file.write_text("not-a-valid-secret-token-xyz123\n", encoding="utf-8")
            if os.name != "nt":
                link_file.chmod(0o600)
            args = _up_args(td, link_file=str(link_file), config_only=True)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cchouse, "_stdin_is_tty", return_value=False), \
                    redirect_stdout(out), redirect_stderr(err):
                rc = cchouse.cmd_up(args)
            self.assertNotEqual(rc, 0)
            combined = out.getvalue() + err.getvalue()
            self.assertIn("节点链接校验失败", combined)
            self.assertNotIn("xyz123", combined)

    def test_existing_config_ignores_link_file_unless_reconfigure(self):
        with tempfile.TemporaryDirectory() as td:
            priv = Path(td) / "priv" / "cc01"
            priv.mkdir(parents=True)
            env_path = priv / "instance.env"
            env_path.write_text("TAG=cc01\nSINGLE_SERVER='198.51.100.1'\n", encoding="utf-8")
            link_file = Path(td) / "new-node.txt"
            link_file.write_text("socks5://u:p@203.0.113.99:1080\n", encoding="utf-8")
            if os.name != "nt":
                link_file.chmod(0o600)

            args = _up_args(td, link_file=str(link_file), config_only=True, reconfigure=False)
            out = io.StringIO()
            with mock.patch.object(cchouse, "_stdin_is_tty", return_value=False), \
                    mock.patch.object(cchouse, "run_preflight_check", return_value=(0, {"ok": True, "status": "PREFLIGHT_OK", "next_action": "proceed_to_render", "warnings": [], "errors": []}, "")), \
                    redirect_stdout(out):
                rc = cchouse.cmd_up(args)
            self.assertEqual(rc, 0)
            self.assertIn("已忽略 --link-file", out.getvalue())
            self.assertIn("198.51.100.1", env_path.read_text(encoding="utf-8"))

            args_reconf = _up_args(td, link_file=str(link_file), config_only=True, reconfigure=True)
            egress = {"ip": "198.51.100.2", "country": "US", "asn": "AS64500", "timezone": "America/Los_Angeles"}
            out2 = io.StringIO()
            with mock.patch.object(cchouse, "_stdin_is_tty", return_value=False), \
                    mock.patch.object(cchouse, "probe_egress_via_proxy", return_value=egress), \
                    mock.patch.object(cchouse, "run_preflight_check", return_value=(0, {"ok": True, "status": "PREFLIGHT_OK", "next_action": "proceed_to_render", "warnings": [], "errors": []}, "")), \
                    redirect_stdout(out2):
                rc2 = cchouse.cmd_up(args_reconf)
            self.assertEqual(rc2, 0)
            self.assertIn("203.0.113.99", env_path.read_text(encoding="utf-8"))
            self.assertIn(f"可以删掉 {link_file} 了", out2.getvalue())

    def test_rebuild_prompt_shows_creation_date_and_reconfigure_hint(self):
        with tempfile.TemporaryDirectory() as td:
            priv = Path(td) / "priv" / "cc01"
            priv.mkdir(parents=True)
            (priv / "instance.env").write_text("TAG=cc01\nEXIT_MODE=single\nSINGLE_TYPE=socks5\n", encoding="utf-8")
            (priv / "state.json").write_text(json.dumps({
                "tag": "cc01",
                "route": "tart-linux",
                "private_dir": str(priv.resolve()),
                "vm_backend": "tart",
                "vm_name": "claude-cc01",
                "vm_created_by_up": True,
                "created_at": "2026-10-15T08:30:00+00:00",
                "updated_at": "2026-11-01T08:30:00+00:00",
                "completed_steps": ["up:config", "up:snapshot"],
            }), encoding="utf-8")
            args = _up_args(td, route="tart-linux", backend="tart", config_only=False, dry_run=False, no_probe=True)
            out = io.StringIO()
            with mock.patch.object(cchouse, "_probe_tart_inventory", return_value=([], None)), \
                    mock.patch.object(cchouse, "_probe_tart_target", return_value=(cchouse.PROBE_ABSENT, None, None)), \
                    mock.patch.object(cchouse, "run_pipeline", return_value=0), \
                    mock.patch.object(cchouse.share_mod, "validate_share_path", side_effect=lambda p, **kw: str(p)), \
                    mock.patch.object(cchouse.share_mod, "ensure_share_dir"), \
                    redirect_stdout(out):
                rc = cchouse.cmd_up(args)
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("2026-10-15", text)
            self.assertIn("如果这不是你装的，或想换节点，加 --reconfigure 重新输入", text)


@unittest.skipIf(os.name == "nt", "requires /bin/bash")
class TestMacBootstrapLinkFile(unittest.TestCase):
    def _bootstrap_text(self, source_hash):
        return (
            (ROOT / "scripts" / "bootstrap-macos.sh").read_text(encoding="utf-8")
            .replace("@CCHOUSE_VERSION@", VERSION)
            .replace("@CCHOUSE_SHA256@", source_hash)
        )

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS ditto")
    def test_link_file_works_without_tty_and_resolves_relative_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / "bin"
            binary.mkdir()
            source = root / "source.zip"
            with zipfile.ZipFile(source, "w") as zf:
                zf.writestr("cchouse/scripts/cchouse.py", "# fixture\n")
            text = self._bootstrap_text(hashlib.sha256(source.read_bytes()).hexdigest())
            for variable, name, member, code in (
                ("python_sha", "python", "python/bin/python3",
                 f'[ "$3" != --version ] || {{ echo "cchouse {VERSION}"; exit 0; }}; printf "PY %s\\n" "$*"'),
                ("tart_sha", "tart", "tart.app/Contents/MacOS/tart", "exit 0"),
            ):
                path = root / f"{name}.tar.gz"
                with tarfile.open(path, "w:gz") as archive:
                    data = ("#!/bin/sh\n" + code + "\n").encode()
                    info = tarfile.TarInfo(member)
                    info.mode = 0o755
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                text = re.sub(rf"(?m)^{variable}='[^']+'", f"{variable}='{hashlib.sha256(path.read_bytes()).hexdigest()}'", text)
            mihomo_gz = root / "mihomo.gz"
            with gzip.open(mihomo_gz, "wb") as gz:
                gz.write(b"#!/bin/sh\nexit 0\n")
            text = re.sub(r"(?m)^mihomo_sha='[^']+'", f"mihomo_sha='{hashlib.sha256(mihomo_gz.read_bytes()).hexdigest()}'", text)
            tools = {
                "uname": "echo Darwin",
                "sysctl": "echo 1",
                "sw_vers": "echo 13.0",
                "curl": """for arg in "$@"; do
  case "$arg" in *.zip) kind=source.zip ;; *install_only*.tar.gz) kind=python.tar.gz ;; *mihomo-*.gz) kind=mihomo.gz ;; */tart.tar.gz) kind=tart.tar.gz ;; esac
  dest="$arg"
done
cp "$HOME/$kind" "$dest"
""",
            }
            for name, contents in tools.items():
                tool = binary / name
                tool.write_text(f"#!/bin/sh\n{contents}\n", encoding="utf-8")
                tool.chmod(0o700)
            script = root / "bootstrap.sh"
            script.write_text(text, encoding="utf-8")
            link_file = root / "my-node.txt"
            link_file.write_text("socks5://203.0.113.1:1080\n", encoding="utf-8")
            link_file.chmod(0o600)

            run = subprocess.run(
                ["/bin/bash", str(script), "--link-file", "my-node.txt"],
                cwd=str(root),
                env=dict(os.environ, HOME=td, PATH=f"{binary}:/usr/bin:/bin:/usr/sbin:/sbin"),
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            expected_abs = str((root / "my-node.txt").resolve())
            self.assertIn(f"PY -I scripts/cchouse.py up --tag cc01 --config-only --skip-preflight --link-file {expected_abs}", run.stdout)
            self.assertIn("PY -I scripts/cchouse.py up --tag cc01", run.stdout)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS ditto")
    def test_link_clipboard_works_without_tty_and_passes_flag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / "bin"
            binary.mkdir()
            source = root / "source.zip"
            with zipfile.ZipFile(source, "w") as zf:
                zf.writestr("cchouse/scripts/cchouse.py", "# fixture\n")
            text = self._bootstrap_text(hashlib.sha256(source.read_bytes()).hexdigest())
            for variable, name, member, code in (
                ("python_sha", "python", "python/bin/python3",
                 f'[ "$3" != --version ] || {{ echo "cchouse {VERSION}"; exit 0; }}; printf "PY %s\\n" "$*"'),
                ("tart_sha", "tart", "tart.app/Contents/MacOS/tart", "exit 0"),
            ):
                path = root / f"{name}.tar.gz"
                with tarfile.open(path, "w:gz") as archive:
                    data = ("#!/bin/sh\n" + code + "\n").encode()
                    info = tarfile.TarInfo(member)
                    info.mode = 0o755
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                text = re.sub(rf"(?m)^{variable}='[^']+'", f"{variable}='{hashlib.sha256(path.read_bytes()).hexdigest()}'", text)
            mihomo_gz = root / "mihomo.gz"
            with gzip.open(mihomo_gz, "wb") as gz:
                gz.write(b"#!/bin/sh\nexit 0\n")
            text = re.sub(r"(?m)^mihomo_sha='[^']+'", f"mihomo_sha='{hashlib.sha256(mihomo_gz.read_bytes()).hexdigest()}'", text)
            tools = {
                "uname": "echo Darwin",
                "sysctl": "echo 1",
                "sw_vers": "echo 13.0",
                "curl": """for arg in "$@"; do
  case "$arg" in *.zip) kind=source.zip ;; *install_only*.tar.gz) kind=python.tar.gz ;; *mihomo-*.gz) kind=mihomo.gz ;; */tart.tar.gz) kind=tart.tar.gz ;; esac
  dest="$arg"
done
cp "$HOME/$kind" "$dest"
""",
            }
            for name, contents in tools.items():
                tool = binary / name
                tool.write_text(f"#!/bin/sh\n{contents}\n", encoding="utf-8")
                tool.chmod(0o700)
            script = root / "bootstrap.sh"
            script.write_text(text, encoding="utf-8")

            run = subprocess.run(
                ["/bin/bash", str(script), "--link-clipboard"],
                cwd=str(root),
                env=dict(os.environ, HOME=td, PATH=f"{binary}:/usr/bin:/bin:/usr/sbin:/sbin"),
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("PY -I scripts/cchouse.py up --tag cc01 --config-only --skip-preflight --link-clipboard", run.stdout)
            self.assertIn("PY -I scripts/cchouse.py up --tag cc01", run.stdout)


class TestLinkClipboardMocked(unittest.TestCase):
    def test_clipboard_reads_first_non_empty_line_clears_and_no_leak(self):
        secret_host = "203.0.113.55"
        secret_user = "clip_user_9"
        secret_pass = "clip_pass_999"
        with tempfile.TemporaryDirectory() as td:
            args = _up_args(td, link_clipboard=True, config_only=True)
            egress = {"ip": "198.51.100.5", "country": "US", "asn": "AS64500", "timezone": "America/Los_Angeles"}
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cchouse, "_stdin_is_tty", return_value=False), \
                    mock.patch.object(cchouse, "_read_clipboard_raw", return_value=f"\n  \nsocks5://{secret_user}:{secret_pass}@{secret_host}:1080  \n\n"), \
                    mock.patch.object(cchouse, "clear_clipboard", return_value=True) as clear_mock, \
                    mock.patch.object(cchouse, "probe_egress_via_proxy", return_value=egress), \
                    mock.patch.object(cchouse, "run_preflight_check", return_value=(0, {"ok": True, "status": "PREFLIGHT_OK", "next_action": "proceed_to_render", "warnings": [], "errors": []}, "")), \
                    redirect_stdout(out), redirect_stderr(err):
                rc = cchouse.cmd_up(args)
            self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
            clear_mock.assert_called_once()
            combined = out.getvalue() + err.getvalue()
            self.assertIn("已清空剪贴板", combined)
            priv = Path(td) / "priv" / "cc01"
            state_text = (priv / "state.json").read_text(encoding="utf-8") if (priv / "state.json").is_file() else ""
            for secret in (secret_host, secret_user, secret_pass):
                self.assertNotIn(secret, combined)
                self.assertNotIn(secret, state_text)
            self.assertIn(f"SINGLE_SERVER='{secret_host}'", (priv / "instance.env").read_text(encoding="utf-8"))

    def test_clipboard_non_link_or_empty_returns_fixed_message_without_echo(self):
        for payload in ("", "   \n\t\n", "my-private-clipboard-text-SECRET888\nsecond line"):
            with tempfile.TemporaryDirectory() as td:
                args = _up_args(td, link_clipboard=True, config_only=True)
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(cchouse, "_stdin_is_tty", return_value=False), \
                        mock.patch.object(cchouse, "_read_clipboard_raw", return_value=payload), \
                        mock.patch.object(cchouse, "clear_clipboard") as clear_mock, \
                        redirect_stdout(out), redirect_stderr(err):
                    rc = cchouse.cmd_up(args)
                self.assertNotEqual(rc, 0)
                clear_mock.assert_not_called()
                combined = out.getvalue() + err.getvalue()
                self.assertIn("剪贴板里不是节点链接，请复制后重试", combined)
                self.assertNotIn("SECRET888", combined)
                self.assertNotIn("my-private-clipboard-text", combined)

    def test_clipboard_unavailable_gives_clear_fallback_hint(self):
        with tempfile.TemporaryDirectory() as td:
            args = _up_args(td, link_clipboard=True, config_only=True)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cchouse, "_stdin_is_tty", return_value=False), \
                    mock.patch.object(cchouse, "_read_clipboard_raw", side_effect=RuntimeError("no_display")), \
                    redirect_stdout(out), redirect_stderr(err):
                rc = cchouse.cmd_up(args)
            self.assertNotEqual(rc, 0)
            combined = out.getvalue() + err.getvalue()
            self.assertIn("无法读取剪贴板", combined)
            self.assertIn("--link-file", combined)

    def test_clipboard_unsupported_udp_protocol_rejected_without_echo(self):
        with tempfile.TemporaryDirectory() as td:
            args = _up_args(td, link_clipboard=True, config_only=True)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cchouse, "_stdin_is_tty", return_value=False), \
                    mock.patch.object(cchouse, "_read_clipboard_raw", return_value="hysteria2://secret-pw@example.invalid:443?sni=example.invalid\n"), \
                    mock.patch.object(cchouse, "clear_clipboard") as clear_mock, \
                    redirect_stdout(out), redirect_stderr(err):
                rc = cchouse.cmd_up(args)
            self.assertNotEqual(rc, 0)
            clear_mock.assert_not_called()
            combined = out.getvalue() + err.getvalue()
            self.assertIn("不支持 UDP 协议", combined)
            self.assertNotIn("secret-pw", combined)

    def test_existing_config_ignores_link_clipboard_unless_reconfigure(self):
        with tempfile.TemporaryDirectory() as td:
            priv = Path(td) / "priv" / "cc01"
            priv.mkdir(parents=True)
            env_path = priv / "instance.env"
            env_path.write_text("TAG=cc01\nSINGLE_SERVER='198.51.100.1'\n", encoding="utf-8")
            args = _up_args(td, link_clipboard=True, config_only=True, reconfigure=False)
            out = io.StringIO()
            with mock.patch.object(cchouse, "_stdin_is_tty", return_value=False), \
                    mock.patch.object(cchouse, "_read_clipboard_raw", side_effect=AssertionError("should not read clipboard")), \
                    mock.patch.object(cchouse, "run_preflight_check", return_value=(0, {"ok": True, "status": "PREFLIGHT_OK", "next_action": "proceed_to_render", "warnings": [], "errors": []}, "")), \
                    redirect_stdout(out):
                rc = cchouse.cmd_up(args)
            self.assertEqual(rc, 0)
            self.assertIn("已忽略 --link-clipboard", out.getvalue())

    def test_mutual_exclusion_with_link_file(self):
        with tempfile.TemporaryDirectory() as td:
            args = _up_args(td, link_clipboard=True, link_file=str(Path(td) / "x.txt"), config_only=True)
            out = io.StringIO()
            with mock.patch.object(cchouse, "_stdin_is_tty", return_value=False), redirect_stdout(out):
                rc = cchouse.cmd_up(args)
            self.assertNotEqual(rc, 0)
            self.assertIn("不能同时使用", out.getvalue())


if __name__ == "__main__":
    unittest.main()
