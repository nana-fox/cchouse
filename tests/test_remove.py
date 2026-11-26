import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import cchouse  # noqa: E402


class TestRemove(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(tmp).resolve() / "Application Support" / "cchouse"
        self.private = self.root / "cc01"
        self.private.mkdir(parents=True)
        self.cache = self.root / "cache"
        self.image = self.cache / "images" / "cchouse-debian13-arm64.tvm"
        self.image.parent.mkdir(parents=True)
        self.image.write_bytes(b"image")
        self.part = self.cache / "claude-code-linux-arm64.tgz.part"
        self.part.write_bytes(b"part")
        self.other_file = self.cache / "my backup.iso"
        self.other_file.touch()
        (self.private / "seed.iso").touch()
        (self.private / "rendered").mkdir()
        self.vm_name = "claude-cc01"
        self.snapshot = "claude-cc01-clean"
        self.golden = "cchouse-debian13-arm64-v2.0.0-preview.2"
        self.inventory = {
            name: {"Name": name, "Size": 2, "Running": False} for name in (
                self.vm_name, self.snapshot, self.golden, "claude-other1", "claude-other2-clean-old",
                "claude-cc01-extra", "claude-cc01-clean-old",
            )
        }
        self.write_state()
        self.mock_run = stack.enter_context(mock.patch.object(cchouse.subprocess, "run", side_effect=self.backend))
        stack.enter_context(mock.patch.object(cchouse, "default_private_root", return_value=self.root))

    def write_state(self, **overrides):
        state = {
            "tag": "cc01", "private_dir": str(self.private), "cache_dir": str(self.cache),
            "route": "tart-linux", "vm_backend": "tart", "vm_created_by_up": True,
            "vm_name": self.vm_name, "clean_snapshot": self.snapshot, "golden_name": self.golden,
            "step": "up:done", "status": "DONE",
        }
        state.update(overrides)
        (self.private / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def backend(self, cmd, **kwargs):
        if cmd[:2] == ["tart", "list"]:
            self.assertEqual(cmd, ["tart", "list", "--source", "local", "--format", "json"])
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(list(self.inventory.values())))
        if cmd[0] in ("tart", "bash"):
            self.assertEqual(cmd[-2], "delete")
            del self.inventory[cmd[-1]]
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "powershell":
            self.assertEqual(cmd[-4:-1], ["-CcOp", "delete", "-CcVmx"])
            Path(cmd[-1]).unlink()
            return subprocess.CompletedProcess(cmd, 0)
        self.fail(f"意外的后端命令 {cmd}")

    def remove(self, *, yes=False, purge=False, answer="\n", private_dir=None, tag="cc01"):
        args = types.SimpleNamespace(tag=tag, private_dir=private_dir, yes=yes, purge_cache=purge)
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(answer)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cchouse.cmd_remove(args)
        return rc, out.getvalue()

    def test_preview_and_default_no_or_eof_leave_everything(self):
        for answer in ("\n", ""):
            rc, text = self.remove(answer=answer)
            self.assertEqual(rc, 0, text)
            self.assertIn("[y/N]", text)
            self.assertIn("已取消", text)
            for item in (self.vm_name, self.snapshot, str(self.private), "seed.iso", "rendered"):
                self.assertIn(item, text)
            self.assertIn(self.vm_name, self.inventory)
            self.assertIn(self.snapshot, self.inventory)
            self.assertTrue(self.private.is_dir())
            self.assertTrue(self.image.is_file())

    def test_tart_removes_exclusively_owned_vm_and_retains_cache(self):
        rc, text = self.remove(answer="y\n")
        self.assertEqual(rc, 0, text)
        self.assertFalse(self.private.exists())
        self.assertEqual(set(self.inventory), {
            self.golden, "claude-other1", "claude-other2-clean-old", "claude-cc01-extra", "claude-cc01-clean-old",
        })
        self.assertTrue(self.image.is_file())
        self.assertTrue(self.part.is_file())
        self.assertIn(str(self.cache), text)
        self.assertIn(self.golden, text)
        rc, text = self.remove(yes=True)
        self.assertEqual(rc, 0, text)

    def test_purge_lists_sizes_and_keeps_unrelated_files(self):
        rc, text = self.remove(yes=True, purge=True)
        self.assertEqual(rc, 0, text)
        self.assertNotIn("[y/N]", text)
        self.assertIn(f"{self.golden}（Tart 报告 2 GB）", text)
        self.assertIn(f"{self.image}（5 字节）", text)
        self.assertIn(f"{self.part}（4 字节）", text)
        self.assertNotIn(self.golden, self.inventory)
        self.assertFalse(self.image.exists())
        self.assertFalse(self.part.exists())
        self.assertTrue(self.other_file.exists())

    def test_vmware_uses_exact_vmx_and_reports_embedded_snapshot(self):
        self.write_state(vm_backend="vmware", route="vmware-linux", clean_snapshot="cchouse-clean")
        vmx = self.private / "vm" / self.vm_name / f"{self.vm_name}.vmx"
        vmx.parent.mkdir(parents=True)
        vmx.touch()
        rc, text = self.remove(yes=True)
        self.assertEqual(rc, 0, text)
        self.assertIn("干净快照 cchouse-clean", text)
        self.assertIn(str(vmx), text)
        self.assertEqual(self.mock_run.call_count, 1)
        self.assertEqual(self.mock_run.call_args.args[0][-1], str(vmx))
        self.assertFalse(self.private.exists())
        self.assertTrue(self.image.is_file())

    def test_mismatched_and_unsupported_owners_leave_instance_untouched(self):
        for overrides in (
            {"vm_name": "claude-other1"}, {"clean_snapshot": "claude-other2-clean-old"},
            {"vm_backend": "virtualbox"}, {"tag": "other2"}, {"private_dir": str(self.root)},
            {"vm_backend": None, "vm_name": None, "route": "vmware-linux"},
        ):
            with self.subTest(overrides=overrides):
                self.write_state(**overrides)
                rc, text = self.remove(yes=True, purge=True)
                self.assertEqual(rc, 1, text)
                self.assertIn("手动清理", text)
                self.assertTrue(self.private.exists())
                self.assertIn(self.vm_name, self.inventory)
        self.mock_run.assert_not_called()

    def test_legacy_tart_uses_recorded_clean_clone_name(self):
        state = {"step": "up:done", "route": "tart-linux", "vm_created_by_up": True, "clean_snapshot": self.snapshot}
        (self.private / "state.json").write_text(json.dumps(state), encoding="utf-8")
        rc, text = self.remove(yes=True)
        self.assertEqual(rc, 0, text)
        self.assertNotIn(self.vm_name, self.inventory)
        self.assertNotIn(self.snapshot, self.inventory)
        self.assertTrue(self.image.is_file())

    def test_without_ownership_does_not_remove_same_named_tart_vms(self):
        self.write_state(vm_created_by_up=False)
        rc, text = self.remove(yes=True)
        self.assertEqual(rc, 0, text)
        self.assertIn(self.vm_name, self.inventory)
        self.assertIn(self.snapshot, self.inventory)
        self.assertFalse(self.private.exists())

    def test_unrelated_vmware_directory_blocks_uninstall(self):
        self.write_state(vm_backend="vmware", route="vmware-linux", clean_snapshot="cchouse-clean")
        other = self.private / "vm" / "claude-other2" / "claude-other2.vmx"
        other.parent.mkdir(parents=True)
        other.touch()
        rc, text = self.remove(yes=True)
        self.assertEqual(rc, 1, text)
        self.assertTrue(other.is_file())
        self.mock_run.assert_not_called()

    def test_state_change_after_preview_blocks_uninstall(self):
        def changed(_prompt):
            self.write_state(status="RUNNING")
            return "y"

        with mock.patch("builtins.input", side_effect=changed):
            rc, text = self.remove()
        self.assertEqual(rc, 1, text)
        self.assertIn("确认期间实例状态改变", text)
        self.assertIn(self.vm_name, self.inventory)
        self.assertTrue(self.private.is_dir())

    def test_backend_failure_keeps_state_for_retry(self):
        original = self.mock_run.side_effect

        def fail_delete(cmd, **kwargs):
            if cmd[-2:] == ["delete", self.vm_name]:
                raise subprocess.CalledProcessError(1, cmd)
            return original(cmd, **kwargs)

        self.mock_run.side_effect = fail_delete
        rc, text = self.remove(yes=True)
        self.assertEqual(rc, 1, text)
        self.assertNotIn(self.snapshot, self.inventory)
        self.assertIn(self.vm_name, self.inventory)
        self.assertTrue((self.private / "state.json").is_file())
        self.mock_run.side_effect = original
        rc, text = self.remove(yes=True)
        self.assertEqual(rc, 0, text)
        self.assertFalse(self.private.exists())

    def test_custom_directory_and_download_cache_use_recorded_paths(self):
        self.private = self.root.parent / "测试 VM"
        self.private.mkdir()
        self.cache = self.root.parent / "自定义 cache"
        self.cache.mkdir()
        custom_package = self.cache / "claude-code-linux-arm64.tgz"
        custom_package.touch()
        self.write_state()
        rc, text = self.remove(yes=True, purge=True, private_dir=str(self.private))
        self.assertEqual(rc, 0, text)
        self.assertFalse(custom_package.exists())
        self.assertFalse(self.private.exists())
        self.assertTrue(self.image.exists())

    def test_ignores_no_state_and_invalid_tag_before_backend_access(self):
        (self.private / "state.json").unlink()
        for tag in ("cc01", "../cc01"):
            rc, _text = self.remove(yes=True, purge=True, tag=tag)
            self.assertNotEqual(rc, 0)
            self.assertTrue(self.private.exists())
        self.mock_run.assert_not_called()


@unittest.skipUnless(os.name == "nt", "requires Windows PowerShell")
class TestVmwareDeleteScript(unittest.TestCase):
    def test_stops_and_deletes_only_the_exact_vmx(self):
        script = r"""
. $env:VMWARE_SCRIPT
$ErrorActionPreference = 'Stop'
function Get-CchouseVmrun { return 'Invoke-FakeVmrun' }
function Invoke-FakeVmrun {
  $global:LASTEXITCODE = 0
  if ($args[2] -eq 'list') {
    'Total running VMs: 2'
    $env:OTHER_VMX
    $env:TEST_VMX
    return
  }
  if ($args[3] -cne $env:TEST_VMX) { throw "Unexpected VMX" }
  switch ($args[2]) {
    'stop' { 'STOPPED TARGET' }
    'deleteVM' { Remove-Item -LiteralPath $args[3]; 'DELETED TARGET' }
    default { throw "Unexpected vmrun command" }
  }
}
Remove-CchouseVM $env:TEST_VMX
"""
        with tempfile.TemporaryDirectory() as tmp:
            vmx = Path(tmp) / "target VM.vmx"
            other = vmx.with_name("another VM.vmx")
            vmx.touch()
            other.touch()
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                env=dict(os.environ, VMWARE_SCRIPT=str(ROOT / "scripts" / "vm-vmware.ps1"),
                         TEST_VMX=str(vmx), OTHER_VMX=str(other)),
                capture_output=True, text=True, errors="replace", timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["STOPPED TARGET", "DELETED TARGET"])
            self.assertFalse(vmx.exists())
            self.assertTrue(other.exists())
