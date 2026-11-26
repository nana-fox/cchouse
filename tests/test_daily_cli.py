import argparse
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
import launcher  # noqa: E402
import preflight  # noqa: E402


def _make_state(private_dir, tag="cc01", backend="tart", vm_name="claude-cc01", snapshot=None, launchers=None):
    private_dir.mkdir(parents=True, exist_ok=True)
    snap = snapshot or (f"{vm_name}-clean" if backend == "tart" else cchouse.CLEAN_SNAPSHOT)
    state = {
        "tag": tag,
        "route": "tart-linux" if backend == "tart" else "virtualbox-linux",
        "private_dir": str(private_dir.resolve()),
        "vm_backend": backend,
        "vm_name": vm_name,
        "vm_created_by_up": True,
        "clean_snapshot": snap,
        "launchers": launchers or [],
    }
    (private_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    (private_dir / "instance.env").write_text("GUEST_IP=192.168.64.2\nSSH_PORT=2222\n", encoding="utf-8")
    (private_dir / "id_ed25519").write_text("dummy-key\n", encoding="utf-8")
    return state


class TestDailyCliDispatch(unittest.TestCase):
    def test_status_tart_running_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "privroot"
            pdir = root / "cc01"
            _make_state(pdir, backend="tart")
            with mock.patch.object(cchouse, "default_private_root", return_value=root), \
                 mock.patch.object(cchouse, "_backend_status", return_value=("running", True)):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cchouse.cmd_status(argparse.Namespace(tag="cc01", private_dir=None, json=False))
                self.assertEqual(rc, 0)
                out = buf.getvalue()
                self.assertIn("运行中", out)
                self.assertIn("存在（claude-cc01-clean）", out)

                jbuf = io.StringIO()
                with redirect_stdout(jbuf):
                    rc = cchouse.cmd_status(argparse.Namespace(tag="cc01", private_dir=None, json=True))
                self.assertEqual(rc, 0)
                data = json.loads(jbuf.getvalue())
                self.assertEqual(data["state"], "running")
                self.assertTrue(data["snapshot_exists"])

    def test_status_missing_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "privroot"
            root.mkdir()
            with mock.patch.object(cchouse, "default_private_root", return_value=root):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cchouse.cmd_status(argparse.Namespace(tag="cc09", private_dir=None, json=False))
                self.assertEqual(rc, 0)
                self.assertIn("不存在", buf.getvalue())

    def test_open_dispatches_tart_and_virtualbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "privroot"
            p_mac = root / "cc01"
            _make_state(p_mac, tag="cc01", backend="tart", vm_name="claude-cc01")
            p_win = root / "cc02"
            _make_state(p_win, tag="cc02", backend="virtualbox", vm_name="claude-cc02")

            with mock.patch.object(cchouse, "default_private_root", return_value=root), \
                 mock.patch.object(cchouse, "_backend_status", return_value=("stopped", True)), \
                 mock.patch.object(cchouse.subprocess, "run") as m_run:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cchouse.cmd_open(argparse.Namespace(tag="cc01", private_dir=None))
                self.assertEqual(rc, 0)
                self.assertIn("Safe Shutdown", buf.getvalue())
                self.assertIn("cchouse stop", buf.getvalue())
                cmd = m_run.call_args[0][0]
                self.assertEqual(cmd[0], "bash")
                self.assertTrue(cmd[1].endswith("vm-tart.sh"))
                self.assertEqual(cmd[2], "open")
                self.assertEqual(cmd[3], "claude-cc01")

                rc = cchouse.cmd_open(argparse.Namespace(tag="cc02", private_dir=None))
                self.assertEqual(rc, 0)
                cmd = m_run.call_args[0][0]
                self.assertEqual(cmd[0], "powershell")
                self.assertIn("-CcOp", cmd)
                self.assertEqual(cmd[cmd.index("-CcOp") + 1], "gui")
                self.assertEqual(cmd[cmd.index("-CcVmName") + 1], "claude-cc02")

    def test_stop_graceful_ssh_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "privroot"
            _make_state(root / "cc01", backend="tart")
            with mock.patch.object(cchouse, "default_private_root", return_value=root), \
                 mock.patch.object(cchouse, "_backend_status", return_value=("running", True)), \
                 mock.patch.object(cchouse, "_ssh_graceful_poweroff", return_value=True) as m_ssh, \
                 mock.patch.object(cchouse, "_wait_stopped", return_value=True):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cchouse.cmd_stop(argparse.Namespace(tag="cc01", private_dir=None, force=False, timeout=30))
                self.assertEqual(rc, 0)
                m_ssh.assert_called_once()
                self.assertIn("已安全关机", buf.getvalue())

    def test_stop_fallback_virtualbox_acpi_and_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "privroot"
            _make_state(root / "cc02", tag="cc02", backend="virtualbox", vm_name="claude-cc02")
            with mock.patch.object(cchouse, "default_private_root", return_value=root), \
                 mock.patch.object(cchouse, "_backend_status", return_value=("running", True)), \
                 mock.patch.object(cchouse, "_ssh_graceful_poweroff", return_value=False), \
                 mock.patch.object(cchouse.subprocess, "run") as m_run:
                with redirect_stdout(io.StringIO()):
                    rc = cchouse.cmd_stop(argparse.Namespace(tag="cc02", private_dir=None, force=False, timeout=45))
                self.assertEqual(rc, 0)
                cmd = m_run.call_args[0][0]
                self.assertEqual(cmd[cmd.index("-CcOp") + 1], "stop")
                self.assertEqual(cmd[cmd.index("-CcForce") + 1], "no")
                self.assertEqual(cmd[cmd.index("-CcTimeout") + 1], "45")

                with redirect_stdout(io.StringIO()):
                    rc = cchouse.cmd_stop(argparse.Namespace(tag="cc02", private_dir=None, force=True, timeout=45))
                self.assertEqual(rc, 0)
                cmd = m_run.call_args[0][0]
                self.assertEqual(cmd[cmd.index("-CcForce") + 1], "yes")

    def test_stop_mac_ssh_unreachable_refuses_hard_stop_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "privroot"
            _make_state(root / "cc01", backend="tart")
            with mock.patch.object(cchouse, "default_private_root", return_value=root), \
                 mock.patch.object(cchouse, "_backend_status", return_value=("running", True)), \
                 mock.patch.object(cchouse, "_ssh_graceful_poweroff", return_value=False), \
                 mock.patch.object(cchouse.subprocess, "run") as m_run:
                err = io.StringIO()
                with redirect_stderr(err):
                    rc = cchouse.cmd_stop(argparse.Namespace(tag="cc01", private_dir=None, force=False, timeout=10))
                self.assertNotEqual(rc, 0)
                m_run.assert_not_called()
                self.assertIn("--force", err.getvalue())

                with redirect_stdout(io.StringIO()):
                    rc = cchouse.cmd_stop(argparse.Namespace(tag="cc01", private_dir=None, force=True, timeout=10))
                self.assertEqual(rc, 0)
                cmd = m_run.call_args[0][0]
                self.assertEqual(cmd[2], "stop")

    def test_restore_tart_updates_identity_and_preserves_env_and_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "privroot"
            pdir = root / "cc01"
            _make_state(pdir, backend="tart")
            env_before = (pdir / "instance.env").read_text(encoding="utf-8")
            key_before = (pdir / "id_ed25519").read_text(encoding="utf-8")
            with mock.patch.object(cchouse, "default_private_root", return_value=root), \
                 mock.patch.object(cchouse, "_backend_status", return_value=("stopped", True)), \
                 mock.patch.object(cchouse, "tart_vm_identity", return_value=[123, 456]), \
                 mock.patch.object(cchouse.subprocess, "run") as m_run:
                with redirect_stdout(io.StringIO()):
                    rc = cchouse.cmd_restore(argparse.Namespace(tag="cc01", private_dir=None, yes=True))
                self.assertEqual(rc, 0)
                cmd = m_run.call_args[0][0]
                self.assertEqual(cmd[2:], ["restore", "claude-cc01", "claude-cc01-clean"])
            updated = json.loads((pdir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(updated["tart_vm_identity"], [123, 456])
            self.assertEqual((pdir / "instance.env").read_text(encoding="utf-8"), env_before)
            self.assertEqual((pdir / "id_ed25519").read_text(encoding="utf-8"), key_before)

    def test_restore_virtualbox_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "privroot"
            _make_state(root / "cc02", tag="cc02", backend="virtualbox", vm_name="claude-cc02")
            with mock.patch.object(cchouse, "default_private_root", return_value=root), \
                 mock.patch.object(cchouse, "_backend_status", return_value=("stopped", True)), \
                 mock.patch.object(cchouse.subprocess, "run") as m_run:
                with redirect_stdout(io.StringIO()):
                    rc = cchouse.cmd_restore(argparse.Namespace(tag="cc02", private_dir=None, yes=True))
                self.assertEqual(rc, 0)
                cmd = m_run.call_args[0][0]
                self.assertEqual(cmd[cmd.index("-CcOp") + 1], "restore")
                self.assertEqual(cmd[cmd.index("-CcVmName") + 1], "claude-cc02")
                self.assertEqual(cmd[cmd.index("-CcSnapshot") + 1], cchouse.CLEAN_SNAPSHOT)

    def test_backend_status_virtualbox_parses_stdout(self):
        info = {"backend": "virtualbox", "vm_name": "claude-cc02", "snapshot": "cchouse-clean"}
        fake = subprocess.CompletedProcess([], 0, stdout="VM_STATE=running-headless\nSNAPSHOT_STATE=present\n", stderr="")
        with mock.patch.object(cchouse.subprocess, "run", return_value=fake):
            st, snap = cchouse._backend_status(info)
        self.assertEqual(st, "running-headless")
        self.assertTrue(snap)

    def test_sync_tart_ip_remaps_pinned_known_hosts_and_quotes_space_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "Application Support" / "cchouse" / "cc01"
            _make_state(pdir, backend="tart")
            kh = pdir / "known_hosts"
            kh.write_text("192.168.64.46 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey\n", encoding="utf-8")
            info = {
                "backend": "tart",
                "vm_name": "claude-cc01",
                "env_path": pdir / "instance.env",
                "known_hosts": kh,
                "ssh_key": pdir / "id_ed25519",
            }
            fake_ip = subprocess.CompletedProcess([], 0, stdout="192.168.64.47\n", stderr="")
            with mock.patch.object(cchouse.subprocess, "run", return_value=fake_ip):
                ip = cchouse._sync_tart_ip_and_known_hosts(info)
            self.assertEqual(ip, "192.168.64.47")
            self.assertIn("GUEST_IP=192.168.64.47", (pdir / "instance.env").read_text(encoding="utf-8"))
            kh_text = kh.read_text(encoding="utf-8")
            self.assertIn("192.168.64.47 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey", kh_text)

            fake_ssh = subprocess.CompletedProcess([], 255, stdout="CCHOUSE_POWEROFF_SENT\n", stderr="")
            with mock.patch.object(cchouse, "_sync_tart_ip_and_known_hosts", return_value="192.168.64.47"), \
                 mock.patch.object(cchouse.subprocess, "run", return_value=fake_ssh) as m_ssh:
                self.assertTrue(cchouse._ssh_graceful_poweroff(info))
                cmd = m_ssh.call_args[0][0]
                self.assertIn(f'UserKnownHostsFile="{kh}"', cmd)


class TestLauncherAndPrivateTart(unittest.TestCase):
    def test_mac_launcher_create_preview_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "Desktop").mkdir()
            state = {"private_dir": str(home / "priv" / "cc01")}

            def fake_run(cmd, **kwargs):
                if cmd[0] == "osacompile":
                    out_app = Path(cmd[cmd.index("-o") + 1])
                    out_app.mkdir(parents=True, exist_ok=True)
                    (out_app / "Contents").mkdir()
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if cmd[0] == "osascript":
                    # Simulate Finder failure -> fallback symlink
                    return subprocess.CompletedProcess(cmd, 1, "", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(launcher.subprocess, "run", side_effect=fake_run):
                paths = launcher.create("cc01", state, platform_name="Darwin", home=home)
            self.assertEqual(len(paths), 2)
            self.assertTrue(Path(paths[0]).is_dir())
            self.assertTrue(Path(paths[1]).is_symlink())
            self.assertEqual(state["launchers"], paths)

            prev = launcher.preview("cc01", state)
            self.assertEqual(len(prev), 2)

            removed = launcher.remove("cc01", state, platform_name="Darwin")
            self.assertEqual(len(removed), 2)
            self.assertFalse(Path(paths[0]).exists())
            self.assertFalse(Path(paths[1]).exists())
            self.assertFalse(Path(paths[1]).is_symlink())
            self.assertEqual(state["launchers"], [])

    def test_windows_launcher_stub_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            launcher.create("cc01", {}, platform_name="Windows")
        with self.assertRaises(NotImplementedError):
            launcher.remove("cc01", {}, platform_name="Windows")

    def test_install_cli_entry_mac(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            tart_dir = home / "Library" / "Application Support" / "cchouse" / "bootstrap" / "tools" / "tart-2.42.0" / "Contents" / "MacOS"
            tart_dir.mkdir(parents=True)
            (tart_dir / "tart").write_text("#!/bin/sh\n", encoding="utf-8")
            with mock.patch.object(launcher.platform, "system", return_value="Darwin"), \
                 mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
                entry, in_path = launcher.install_cli_entry(python_exe="/opt/py/python3", repo_root=ROOT, home=home)
            self.assertFalse(in_path)
            self.assertTrue(entry.is_file())
            content = entry.read_text(encoding="utf-8").replace("\\", "/")
            self.assertIn("scripts/cchouse.py", content)
            self.assertIn("tart-2.42.0/Contents/MacOS", content)
            if os.name != "nt":
                self.assertTrue(bool(entry.stat().st_mode & stat.S_IXUSR))

    def test_find_tart_discovers_private_tart(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            tart_bin = home / "Library" / "Application Support" / "cchouse" / "bootstrap" / "tools" / "tart-2.42.0" / "Contents" / "MacOS" / "tart"
            tart_bin.parent.mkdir(parents=True)
            tart_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            with mock.patch.object(preflight.shutil, "which", return_value=None), \
                 mock.patch.object(preflight.Path, "home", return_value=home), \
                 mock.patch.object(preflight.os.path, "isfile", side_effect=lambda p: Path(p) == tart_bin):
                found = preflight.find_tart()
            self.assertEqual(found, str(tart_bin))


if __name__ == "__main__":
    unittest.main()
