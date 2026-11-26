"""Unit tests for Windows launcher (.lnk + cchouse.cmd) and VirtualBox snapshot handoff."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import launcher  # noqa: E402


class TestWindowsLauncher(unittest.TestCase):
    def test_install_cli_entry_writes_windows_cmd(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            py = home / "python.exe"
            py.write_bytes(b"")
            entry, _in_path = launcher.install_cli_entry(
                python_exe=py, repo_root=ROOT, home=home, platform_name="Windows",
            )
            self.assertEqual(entry, home / "AppData" / "Local" / "cchouse" / "bin" / "cchouse.cmd")
            text = entry.read_text(encoding="utf-8")
            self.assertIn("@echo off", text)
            self.assertIn('set "PYTHONUTF8=1"', text)
            self.assertIn(f'"{py.resolve()}" -I "{(ROOT / "scripts" / "cchouse.py").resolve()}" %*', text)

    def test_windows_create_and_remove_mocked(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            state = {"private_dir": str(home / "priv" / "cc07")}
            created_specs = []

            def fake_create(specs):
                created_specs.extend(specs)
                for item in specs:
                    p = Path(item["path"])
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"fake-lnk")

            with mock.patch.object(launcher, "_create_windows_lnk_files", side_effect=fake_create):
                paths = launcher.create("cc07", state, platform_name="Windows", home=home)

            self.assertEqual(len(paths), 2)
            self.assertEqual(state["launchers"], paths)
            self.assertTrue(any(p.endswith(os.path.join("Desktop", "cchouse (cc07).lnk")) for p in paths))
            self.assertTrue(any("Start Menu" in p and p.endswith("cchouse (cc07).lnk") for p in paths))
            for spec in created_specs:
                self.assertIn("-WindowStyle Hidden", spec["arguments"])
                self.assertIn("cchouse.cmd", spec["arguments"])
                self.assertIn("open --tag 'cc07'", spec["arguments"])
                self.assertIn("--private-dir", spec["arguments"])

            self.assertEqual(len(launcher.preview("cc07", state)), 2)

            # Safety: an unrelated path in state["launchers"] must never be deleted
            unrelated = home / "keep.txt"
            unrelated.write_text("keep", encoding="utf-8")
            state["launchers"].append(str(unrelated))

            removed = launcher.remove("cc07", state, platform_name="Windows")
            self.assertEqual(len(removed), 2)
            self.assertEqual(state["launchers"], [])
            self.assertTrue(unrelated.is_file())
            for p in paths:
                self.assertFalse(Path(p).exists())

    @unittest.skipUnless(os.name == "nt", "requires Windows WScript.Shell")
    def test_windows_real_lnk_create_and_remove(self):
        with tempfile.TemporaryDirectory(prefix="cchouse 中文 ") as td:
            home = Path(td)
            state = {"private_dir": str(home / "priv" / "cc08")}
            paths = launcher.create("cc08", state, platform_name="Windows", home=home)
            self.assertEqual(len(paths), 2)
            for p in paths:
                self.assertTrue(Path(p).is_file())
            removed = launcher.remove("cc08", state, platform_name="Windows")
            self.assertEqual(len(removed), 2)
            for p in paths:
                self.assertFalse(Path(p).exists())


if __name__ == "__main__":
    unittest.main()
