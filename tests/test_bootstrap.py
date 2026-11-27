import base64
import gzip
import hashlib
import importlib.util
import io
import os
import re
import subprocess
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_script("build_bootstrap_release", "tools/build-bootstrap-release.py")
verify = load_script("verify_linux_native", "scripts/verify-linux-native.py")
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()


def bootstrap_text(platform, source_hash):
    name = "bootstrap-macos.sh" if platform == "mac" else "bootstrap-windows.ps1"
    return ((ROOT / "scripts" / name).read_text(encoding="utf-8-sig")
            .replace("@CCHOUSE_VERSION@", VERSION).replace("@CCHOUSE_SHA256@", source_hash))


def source_zip(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("cchouse/scripts/cchouse.py", "# CLI fixture\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BootstrapReleaseTests(unittest.TestCase):
    def test_assets_come_from_frozen_commit_and_pin_each_hash(self):
        commit = "a" * 40
        fixture = b"frozen source ZIP fixture"
        template = b"# @CCHOUSE_VERSION@ @CCHOUSE_SHA256@"
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            release, "run_git", side_effect=[commit.encode(), VERSION.encode(), b"", template, template, fixture],
        ) as git:
            out = Path(td) / "assets"
            release.build(None, "HEAD", out)
            self.assertEqual(git.call_args_list[-1].args[-1], commit)
            source = out / f"cchouse-{VERSION}.zip"
            self.assertEqual(source.read_bytes(), fixture)
            fixtures = [source, out / "bootstrap-macos.sh", out / "bootstrap-windows.ps1"]
            sums = (out / "SHA256SUMS").read_text().splitlines()
            readme = (out / "README-commands.md").read_text()
            for artifact, entry in zip(fixtures, sums):
                digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
                self.assertEqual(entry, f"{digest}  {artifact.name}")
                if artifact != source:
                    self.assertIn(hashlib.sha256(fixture).hexdigest(), artifact.read_text(encoding="utf-8-sig"))
                    self.assertIn(f"/releases/download/{VERSION}/{artifact.name}", readme)
                    self.assertIn(digest, readme)
            self.assertTrue(fixtures[-1].read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertNotIn("ScriptBlock", readme)
            self.assertNotIn("trap '", readme)

    def test_tag_is_not_a_moving_alias(self):
        with mock.patch.object(release, "run_git", side_effect=[b"a" * 40, VERSION.encode()]), \
                self.assertRaisesRegex(ValueError, "does not match"):
            release.build("latest", "HEAD", Path("unused"))


class WindowsNativeVerifyTests(unittest.TestCase):
    def test_native_openssh_uses_strict_host_key_and_sends_no_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            identity = Path(td) / "private path" / "id_ed25519"
            known_hosts = identity.with_name("known_hosts")
            env = {
                "TAG": "cc01", "GUEST_IP": "127.0.0.1", "ROUTER_IP": "192.168.100.1",
                "SSH_PORT": "49732", "SSH_KEY": str(identity), "SSH_KNOWN_HOSTS": str(known_hosts),
                "EXPECTED_EGRESS_IP": "203.0.113.9", "EXPECTED_EGRESS_ASN": "AS64500",
                "SINGLE_PASS": "test-proxy-secret", "SINGLE_UUID": "test-uuid-secret",
            }
            with mock.patch.object(verify.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
                self.assertEqual(verify.verify(env, ["--fail-closed"], "accept-new"), 1)
            args = run.call_args.args[0]
            self.assertIn(str(identity.resolve()), args)
            self.assertIn(f'UserKnownHostsFile="{known_hosts.resolve().as_posix()}"', args)
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("ServerAliveInterval=5", args)
            self.assertEqual(run.call_args.kwargs["timeout"], verify.VERIFY_TIMEOUT_SECONDS)
            if os.name == "nt":
                self.assertIn("-n", args)
                self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(args[-4:-1], ["-p", "49732", "claude@127.0.0.1"])
                self.assertTrue(args[-1].endswith("| bash -s -- --guest-local --fail-closed"))
                m = re.match(r"^printf %s ([A-Za-z0-9+/=]+) \| base64 -d \| gzip -dc \| ", args[-1])
                self.assertIsNotNone(m)
                payload = gzip.decompress(base64.b64decode(m.group(1))).decode("utf-8")
            else:
                payload = run.call_args.kwargs["input"].decode()
                self.assertEqual(args[-4:], ["-p", "49732", "claude@127.0.0.1", "bash -s -- --guest-local --fail-closed"])
            self.assertIn("export EXPECTED_EGRESS_IP=203.0.113.9\n", payload)
            self.assertIn("export CCHOUSE_HOSTKEY_TRUST=accept-new\n", payload)
            self.assertIn("restore_mihomo", payload)
            for value in ("test-proxy-secret", "test-uuid-secret", str(identity), str(known_hosts)):
                self.assertNotIn(value, payload)

    def test_ssh_timeout_exits_nonzero_without_printing_command(self):
        timeout = subprocess.TimeoutExpired(["ssh", "internal command"], verify.VERIFY_TIMEOUT_SECONDS)
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            sys, "argv", ["verify-linux-native.py", str(Path(td) / "instance.env"), "--fail-closed"],
        ), mock.patch.object(verify.render, "load_env", return_value={}), mock.patch.object(
            verify, "verify", side_effect=timeout,
        ), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(verify.main(), 124)
            self.assertIn("已终止", err.getvalue())
            self.assertNotIn("internal command", err.getvalue())

    def test_invalid_forward_port_exits_before_openssh(self):
        with mock.patch.object(verify.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "SSH_PORT"):
            verify.verify({"TAG": "cc01", "GUEST_IP": "127.0.0.1", "ROUTER_IP": "192.168.100.1",
                           "SSH_PORT": "65536"}, [])
        run.assert_not_called()


@unittest.skipIf(os.name == "nt", "requires /bin/bash")
class MacBootstrapTests(unittest.TestCase):
    def test_mismatched_source_aborts_before_installing_tools(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / "bin"
            binary.mkdir()
            for name, body in {
                "uname": "echo Darwin",
                "sysctl": "echo 1",
                "sw_vers": "echo 13.0",
                "curl": 'for arg in "$@"; do dest="$arg"; done; printf tampered > "$dest"',
            }.items():
                command = binary / name
                command.write_text(f"#!/bin/sh\n{body}\n")
                command.chmod(0o700)
            script = root / "bootstrap.sh"
            script.write_text(bootstrap_text("mac", "a" * 64), encoding="utf-8")
            saved_env = root / "Library/Application Support/cchouse/cc01/instance.env"
            saved_env.parent.mkdir(parents=True, exist_ok=True)
            saved_env.write_text("TAG=cc01\n", encoding="utf-8")
            run = subprocess.run(
                ["/bin/bash", script], env=dict(os.environ, HOME=td, PATH=f"{binary}:/usr/bin:/bin:/usr/sbin:/sbin"),
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            self.assertIn("SHA-256 校验失败", run.stderr)
            self.assertIn("检测到已有节点配置", run.stdout)
            self.assertNotIn("按提示输入节点链接", run.stdout)
            bootstrap = root / "Library/Application Support/cchouse/bootstrap"
            self.assertFalse(any((bootstrap / "tools").iterdir()))
            self.assertFalse((bootstrap / ".prepare").exists())

    def test_non_tty_without_saved_env_exits_20_with_home_cmd_and_no_downloads(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / "bin"
            binary.mkdir()
            for name, body in {
                "uname": "echo Darwin",
                "sysctl": "echo 1",
                "sw_vers": "echo 13.0",
                "curl": 'echo "curl should not run" >&2; exit 99',
            }.items():
                command = binary / name
                command.write_text(f"#!/bin/sh\n{body}\n")
                command.chmod(0o700)
            script = root / "bootstrap.sh"
            script.write_text(bootstrap_text("mac", "a" * 64), encoding="utf-8")
            self_sha = hashlib.sha256(script.read_bytes()).hexdigest()
            run = subprocess.run(
                ["/bin/bash", script], env=dict(os.environ, HOME=td, PATH=f"{binary}:/usr/bin:/bin:/usr/sbin:/sbin"),
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=20,
            )
            self.assertEqual(run.returncode, 20, run.stdout + run.stderr)
            self.assertIn("需要你本人输入节点链接：请在你自己的终端窗口运行下面这条命令（AI 无法也不应替你输入）", run.stdout)
            self.assertIn(f"/releases/download/{VERSION}/bootstrap-macos.sh", run.stdout)
            self.assertIn(self_sha, run.stdout)
            self.assertFalse((root / "Library/Application Support/cchouse/bootstrap").exists())

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS ditto")
    def test_private_paths_with_chinese_and_spaces_are_reused_on_rerun(self):
        with tempfile.TemporaryDirectory(prefix="cchouse 中文 ") as td:
            root = Path(td)
            binary = root / "bin"
            binary.mkdir()
            source = root / "source.zip"
            text = bootstrap_text("mac", source_zip(source))
            for variable, name, member, code in (
                ("python_sha", "python", "python/bin/python3",
                 f'[ "$3" != --version ] || {{ echo "cchouse {VERSION}"; exit 0; }}; printf "UP %s\\n" "$*"'),
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
            tools = {
                "uname": "echo Darwin",
                "sysctl": "echo 1",
                "sw_vers": "echo 13.0",
                "curl": """for arg in "$@"; do
  case "$arg" in *.zip) kind=source.zip ;; *install_only*.tar.gz) kind=python.tar.gz ;; */tart.tar.gz) kind=tart.tar.gz ;; esac
  dest="$arg"
done
printf '%s\n' "$kind" >> "$HOME/downloads.log"
cp "$HOME/$kind" "$dest"
""",
            }
            for name, contents in tools.items():
                tool = binary / name
                tool.write_text(f"#!/bin/sh\n{contents}\n")
                tool.chmod(0o700)
            script = root / "launch.sh"
            script.write_text(text, encoding="utf-8")
            env = dict(os.environ, HOME=td, PATH=f"{binary}:/usr/bin:/bin:/usr/sbin:/sbin")
            for args, tag in (([], "cc01"), (["--tag", "cc02"], "cc02")):
                saved_env = root / "Library/Application Support/cchouse" / tag / "instance.env"
                saved_env.parent.mkdir(parents=True, exist_ok=True)
                saved_env.write_text(f"TAG={tag}\n", encoding="utf-8")
                run = subprocess.run(
                    ["/bin/bash", script, *args], env=env, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, encoding="utf-8", timeout=30,
                )
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertIn(f"UP -I scripts/cchouse.py up --tag {tag}", run.stdout)
            self.assertEqual((root / "downloads.log").read_text().splitlines(), ["source.zip", "python.tar.gz", "tart.tar.gz"])


@unittest.skipUnless(os.name == "nt", "requires Windows PowerShell")
class WindowsBootstrapTests(unittest.TestCase):
    def test_missing_openssh_or_wrong_source_hash_cannot_launch_up(self):
        driver = """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
function Invoke-WebRequest {
  param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile)
  [IO.File]::WriteAllText($OutFile, 'tampered')
}
& $env:TEST_BOOTSTRAP
exit $LASTEXITCODE
"""
        encoded = base64.b64encode(driver.encode("utf-16-le")).decode("ascii")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "bootstrap.ps1"
            script.write_text(bootstrap_text("windows", "a" * 64).replace("$env:WINDIR", "$env:TEST_ROOT"),
                              encoding="utf-8-sig")
            system32 = root / "System32" / "OpenSSH"
            system32.mkdir(parents=True)
            saved_env = root / "cchouse" / "cc01" / "instance.env"
            saved_env.parent.mkdir(parents=True, exist_ok=True)
            saved_env.write_text("TAG=cc01\n", encoding="utf-8")
            bootstrap = root / "cchouse" / "bootstrap"
            for openssh_available in (False, True):
                if openssh_available:
                    for name in ("ssh.exe", "ssh-keygen.exe"):
                        (system32 / name).touch()
                run = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                    env=dict(os.environ, TEST_ROOT=td, LOCALAPPDATA=td, TEST_BOOTSTRAP=str(script)),
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                )
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                if not openssh_available:
                    self.assertIn("缺少 OpenSSH 客户端", run.stdout)
                    self.assertFalse(bootstrap.exists())
                else:
                    self.assertIn("SHA-256 校验失败", run.stdout)
                    self.assertIn("检测到已有节点配置", run.stdout)
                    self.assertNotIn("按提示输入节点链接", run.stdout)
                    self.assertFalse(any((bootstrap / "tools").iterdir()))

    def test_non_tty_without_saved_env_exits_20_with_home_cmd_and_no_downloads(self):
        driver = """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
& $env:TEST_BOOTSTRAP
exit $LASTEXITCODE
"""
        encoded = base64.b64encode(driver.encode("utf-16-le")).decode("ascii")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "bootstrap.ps1"
            script.write_text(bootstrap_text("windows", "a" * 64).replace("$env:WINDIR", "$env:TEST_ROOT"),
                              encoding="utf-8-sig")
            self_hash = hashlib.sha256(script.read_bytes()).hexdigest()
            system32 = root / "System32" / "OpenSSH"
            system32.mkdir(parents=True)
            for name in ("ssh.exe", "ssh-keygen.exe"):
                (system32 / name).touch()
            run = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                env=dict(os.environ, TEST_ROOT=td, LOCALAPPDATA=td, TEST_BOOTSTRAP=str(script)),
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertEqual(run.returncode, 20, run.stdout + run.stderr)
            self.assertIn("需要你本人输入节点链接：请在你自己的终端窗口运行下面这条命令（AI 无法也不应替你输入）", run.stdout)
            self.assertIn(f"/releases/download/{VERSION}/bootstrap-windows.ps1", run.stdout)
            self.assertIn(self_hash, run.stdout)
            self.assertFalse((root / "cchouse" / "bootstrap").exists())

    def test_chinese_spaced_path_reruns_without_download_and_propagates_exit(self):
        compile_exe = """
Add-Type -OutputType ConsoleApplication -OutputAssembly $env:TEST_EXE -TypeDefinition @'
using System;
public class BootstrapFixture {
  public static int Main(string[] args) {
    if (Array.IndexOf(args, "--version") >= 0) {
      Console.WriteLine("cchouse " + Environment.GetEnvironmentVariable("TEST_VERSION"));
      return 0;
    }
    Console.WriteLine("UP " + String.Join(" ", args));
    return 7;
  }
}
'@
"""
        driver = r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PATH = "$PSHOME;$env:WINDIR\System32"
if (Get-Command git -ErrorAction SilentlyContinue) { throw 'Git should be absent from PATH' }
function Invoke-WebRequest {
  param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile)
  $name = if ($Uri -like '*embed-amd64.zip') { 'python.zip' } else { 'source.zip' }
  Add-Content -LiteralPath (Join-Path $env:TEST_ROOT 'downloads.log') $name
  Copy-Item -LiteralPath (Join-Path $env:TEST_ROOT $name) -Destination $OutFile
}
& $env:TEST_BOOTSTRAP
exit $LASTEXITCODE
"""

        def run_ps(text, env):
            encoded = base64.b64encode(text.encode("utf-16-le")).decode("ascii")
            return subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )

        with tempfile.TemporaryDirectory(prefix="cchouse 中文 ") as td:
            root = Path(td)
            exe, script = root / "python.exe", root / "bootstrap.ps1"
            env = dict(os.environ, TEST_ROOT=td, LOCALAPPDATA=td, TEST_BOOTSTRAP=str(script), TEST_EXE=str(exe),
                       TEST_VERSION=VERSION)
            compile_result = run_ps(compile_exe, env)
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            python = root / "python.zip"
            with zipfile.ZipFile(python, "w") as archive:
                archive.write(exe, "python.exe")
            script_text = bootstrap_text("windows", source_zip(root / "source.zip")).replace("$env:WINDIR", "$env:TEST_ROOT")
            python_hash = hashlib.sha256(python.read_bytes()).hexdigest()
            script_text = re.sub(r"(?m)^\$pythonHash = '[^']+'", f"$pythonHash = '{python_hash}'", script_text)
            script.write_text(script_text, encoding="utf-8-sig")
            ssh = root / "System32/OpenSSH"
            ssh.mkdir(parents=True)
            for name in ("ssh.exe", "ssh-keygen.exe"):
                (ssh / name).touch()
            saved_env = root / "cchouse" / "cc01" / "instance.env"
            saved_env.parent.mkdir(parents=True, exist_ok=True)
            saved_env.write_text("TAG=cc01\n", encoding="utf-8")
            for _ in range(2):
                run = run_ps(driver, env)
                self.assertEqual(run.returncode, 7, run.stdout + run.stderr)
                self.assertIn("UP -I scripts/cchouse.py up --tag cc01 --vm-backend virtualbox", run.stdout)
            self.assertEqual((root / "downloads.log").read_text(encoding="ascii").splitlines(), ["source.zip", "python.zip"])
