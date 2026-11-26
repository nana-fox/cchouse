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
            self.assertEqual(cmd[-4:-1], ["-CcOp", "delete", "-CcVmName"])
            self.assertEqual(cmd[-1], self.vm_name)
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

    def test_old_windows_backend_leaves_vm_and_private_data_intact(self):
        self.write_state(vm_backend="vmware", route="vmware-linux", clean_snapshot="cchouse-clean")
        disk = self.private / "vm" / self.vm_name / "guest-disk.vmdk"
        disk.parent.mkdir(parents=True)
        disk.write_bytes(b"existing guest")
        rc, text = self.remove(yes=True, purge=True)
        self.assertEqual(rc, 1, text)
        self.assertEqual(disk.read_bytes(), b"existing guest")
        self.assertTrue(self.image.is_file())
        self.mock_run.assert_not_called()

    def test_mismatched_and_unsupported_owners_leave_instance_untouched(self):
        for overrides in (
            {"vm_name": "claude-other1"}, {"clean_snapshot": "claude-other2-clean-old"},
            {"vm_backend": "virtualbox", "vm_name": "claude-other1", "clean_snapshot": "cchouse-clean"},
            {"tag": "other2"}, {"private_dir": str(self.root)},
            {"vm_backend": None, "vm_name": None, "route": "virtualbox-linux"},
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

    def test_virtualbox_deletes_the_exact_recorded_name(self):
        self.write_state(vm_backend="virtualbox", route="virtualbox-linux", clean_snapshot="cchouse-clean")
        vm_dir = self.private / "vm" / self.vm_name
        vm_dir.mkdir(parents=True)
        (vm_dir / f"{self.vm_name}.vbox").touch()
        rc, text = self.remove(yes=True)
        self.assertEqual(rc, 0, text)
        self.assertIn("unregistervm --delete", text)
        self.assertIn(self.vm_name, text)
        self.assertIn("先关机", text)
        self.assertFalse(self.private.exists())
        self.assertEqual(self.mock_run.call_count, 1)
        cmd = self.mock_run.call_args.args[0]
        self.assertEqual(cmd[-4:], ["-CcOp", "delete", "-CcVmName", self.vm_name])
        self.assertTrue(cmd[-5].endswith("vm-virtualbox.ps1"))
        self.assertTrue(self.image.is_file())

    def test_unrelated_virtualbox_directory_blocks_uninstall(self):
        self.write_state(vm_backend="virtualbox", route="virtualbox-linux", clean_snapshot="cchouse-clean")
        other = self.private / "vm" / "claude-other2"
        other.mkdir(parents=True)
        rc, text = self.remove(yes=True)
        self.assertEqual(rc, 1, text)
        self.assertIn("无法确认归属", text)
        self.assertTrue(other.is_dir())
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


@unittest.skipIf(os.name == "nt", "requires Bash wrapper")
class TestTartDeleteScript(unittest.TestCase):
    def test_stops_running_guest_or_clean_clone_before_delete(self):
        for name, list_fails in (("claude-cc01", False), ("claude-cc01-clean", False), ("claude-cc01", True)):
            with self.subTest(name=name, list_fails=list_fails), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "calls"
                tart = Path(tmp) / "tart"
                tart.write_text(r"""#!/bin/bash
printf '%s\n' "$*" >> "$TEST_TART_CALLS"
case "$1" in
  stop) exit 0 ;;
  list)
    if [ "$TEST_LIST_FAILS" = "yes" ]; then exit 1; fi
    printf 'local %s 26 GB 3 GB today stopped\n' "$TEST_VM_NAME"
    ;;
  delete) exit 0 ;;
  *) exit 2 ;;
esac
""", encoding="utf-8")
                tart.chmod(0o700)
                result = subprocess.run(
                    ["bash", str(ROOT / "scripts" / "vm-tart.sh"), "delete", name],
                    env=dict(os.environ, PATH=f"{tmp}{os.pathsep}{os.environ['PATH']}",
                             TEST_TART_CALLS=str(log), TEST_VM_NAME=name, TEST_LIST_FAILS="yes" if list_fails else "no"),
                    capture_output=True, text=True, timeout=10,
                )
                expected = [f"stop {name}", "list --source local"]
                if not list_fails:
                    expected.append(f"delete {name}")
                self.assertEqual(log.read_text(encoding="utf-8").splitlines(), expected)
                self.assertEqual(result.returncode, int(list_fails), result.stderr)
