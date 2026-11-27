import argparse
import io
import json
import os
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


def _make_fake_tart(td, name="tart", executable=True):
    exe = Path(td) / name
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    if executable:
        exe.chmod(0o700)
    else:
        exe.chmod(0o600)
    return str(exe.resolve())


def _make_env(private_dir, tag="cc01", ip="203.0.113.9", loc="US", asn="AS64500", tz="America/Los_Angeles"):
    private_dir.mkdir(parents=True, exist_ok=True)
    env_text = (
        "GUEST_OS=linux\n"
        "EXIT_MODE=single\n"
        "STRIPE_EXIT_MODE=off\n"
        f"TAG={tag}\n"
        f"VM_NAME=claude-{tag}\n"
        "GUEST_IF=enp0s1\n"
        "MIHOMO_USER=mihomo\n"
        f'PRIVATE_DIR="{private_dir}"\n'
        f'SSH_KEY="{private_dir / "id_ed25519"}"\n'
        f'SSH_KNOWN_HOSTS="{private_dir / "known_hosts"}"\n'
        "SSH_PUBKEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey cchouse'\n"
        "VMNET_CIDR=192.168.64.0/24\n"
        "GUEST_IP=192.168.64.2\n"
        "ROUTER_IP=192.168.100.1\n"
        "HOME_LAN_CIDR=192.168.100.0/24\n"
        f"TIMEZONE={tz}\n"
        "GUEST_LOCALE=en_US.UTF-8\n"
        "GUEST_KEYBOARD=us\n"
        "GUEST_HOSTNAME=''\n"
        f"EXPECTED_EGRESS_IP={ip}\n"
        f"EXPECTED_EGRESS_LOC={loc}\n"
        f"EXPECTED_EGRESS_ASN={asn}\n"
        "SINGLE_TYPE=socks5\n"
        "SINGLE_SERVER='203.0.113.8'\n"
        "SINGLE_SERVER_IP='203.0.113.8'\n"
        "SINGLE_PORT='1080'\n"
        "SINGLE_USER='u'\n"
        "SINGLE_PASS='p'\n"
        "SINGLE_TLS=''\n"
    )
    (private_dir / "instance.env").write_text(env_text, encoding="utf-8")
    (private_dir / "id_ed25519").write_text("dummy-private-key\n", encoding="utf-8")
    (private_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey cchouse\n", encoding="utf-8")
    (private_dir / "known_hosts").write_text("192.168.64.2 ssh-ed25519 AAAAC3OldKey\n", encoding="utf-8")
    (private_dir / "guest_ip.txt").write_text("192.168.64.2", encoding="utf-8")
    logs = private_dir / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "hostkey.serial").write_text("old-serial\n", encoding="utf-8")
    (logs / "hostkey.trust").write_text("pinned\n", encoding="utf-8")


def _up_args(td, tag="cc01", route="tart-linux", backend="tart", **over):
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
        link_stdin=False,
        non_interactive=False,
        no_probe=False,
        vm_backend=backend,
        reconfigure=False,
        share=None,
        read_only=False,
        config_only=False,
        dry_run=False,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


class TestTriStateProbing(unittest.TestCase):
    def test_tart_non_executable_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            non_exec = _make_fake_tart(td, executable=False)
            th = Path(td) / "tart-home"
            th.mkdir()
            with mock.patch.dict(os.environ, {"TART_HOME": str(th)}), \
                    mock.patch.object(cchouse.os, "access", return_value=False):
                state, reason, _ = cchouse._probe_tart_target("claude-cc01", non_exec)
            self.assertEqual(state, cchouse.PROBE_UNKNOWN)
            self.assertIn("无法执行", reason)

    def test_tart_missing_binary_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            missing = str(Path(td) / "no-such-tart")
            state, reason, _ = cchouse._probe_tart_target("claude-cc01", missing)
            self.assertEqual(state, cchouse.PROBE_UNKNOWN)
            self.assertIn("找不到或无法执行", reason)

    def test_tart_offline_home_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            exe = _make_fake_tart(td)
            offline = str(Path(td) / "unmounted-disk" / "tart")
            with mock.patch.dict(os.environ, {"TART_HOME": offline}), \
                    mock.patch.object(cchouse.subprocess, "run") as m_run:
                state, reason, _ = cchouse._probe_tart_target("claude-cc01", exe)
            self.assertEqual(state, cchouse.PROBE_UNKNOWN)
            self.assertIn("无法访问 Tart 目录", reason)
            m_run.assert_not_called()

    def test_tart_list_failure_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            exe = _make_fake_tart(td)
            th = Path(td) / "tart-home"
            th.mkdir()
            with mock.patch.dict(os.environ, {"TART_HOME": str(th)}), \
                    mock.patch.object(cchouse.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="io error")):
                state, reason, _ = cchouse._probe_tart_target("claude-cc01", exe)
            self.assertEqual(state, cchouse.PROBE_UNKNOWN)
            self.assertIn("列表查询失败", reason)

    def test_tart_confirmed_absent_and_present(self):
        with tempfile.TemporaryDirectory() as td:
            exe = _make_fake_tart(td)
            th = Path(td) / "tart-home"
            th.mkdir()
            inv = json.dumps([{"Name": "claude-cc01", "State": "stopped"}])
            with mock.patch.dict(os.environ, {"TART_HOME": str(th)}), \
                    mock.patch.object(cchouse.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=inv)):
                s1, _, _ = cchouse._probe_tart_target("claude-cc01", exe)
                s2, _, _ = cchouse._probe_tart_target("claude-cc02", exe)
            self.assertEqual(s1, cchouse.PROBE_PRESENT)
            self.assertEqual(s2, cchouse.PROBE_ABSENT)

    def test_vbox_query_failure_inaccessible_and_remnants_are_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            pdir = Path(td) / "cc01"
            pdir.mkdir()
            # 1. VBoxManage query fails (exit 1)
            with mock.patch.object(cchouse.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="VBoxManage not found")):
                res = cchouse._probe_vbox_status("claude-cc01", "cchouse-clean", pdir)
            self.assertEqual(res["vm"], cchouse.PROBE_UNKNOWN)
            self.assertEqual(res["vm_state"], "unknown")

            # 2. VM inaccessible
            with mock.patch.object(cchouse.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="VM_STATE=inaccessible\nSNAPSHOT_STATE=unknown\n")):
                res2 = cchouse._probe_vbox_status("claude-cc01", "cchouse-clean", pdir)
            self.assertEqual(res2["vm"], cchouse.PROBE_UNKNOWN)
            self.assertIn("inaccessible", res2["reason"])

            # 3. Unregistered remnants under private_dir/vm/
            (pdir / "vm" / "claude-cc01").mkdir(parents=True)
            (pdir / "vm" / "claude-cc01" / "claude-cc01.vbox").write_text("<xml/>", encoding="utf-8")
            with mock.patch.object(cchouse.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="VM_STATE=missing\nSNAPSHOT_STATE=missing\n")):
                res3 = cchouse._probe_vbox_status("claude-cc01", "cchouse-clean", pdir)
            self.assertEqual(res3["vm"], cchouse.PROBE_UNKNOWN)
            self.assertIn("残留", res3["reason"])

            # 4. Cleanly absent when vm/ has no remnants
            (pdir / "vm" / "claude-cc01" / "claude-cc01.vbox").unlink()
            (pdir / "vm" / "claude-cc01").rmdir()
            with mock.patch.object(cchouse.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="VM_STATE=missing\nSNAPSHOT_STATE=missing\n")):
                res4 = cchouse._probe_vbox_status("claude-cc01", "cchouse-clean", pdir)
            self.assertEqual(res4["vm"], cchouse.PROBE_ABSENT)


class TestStateCorruption(unittest.TestCase):
    def test_corrupt_state_json_stops_without_overwriting(self):
        with tempfile.TemporaryDirectory() as td:
            args = _up_args(td)
            pdir = Path(args.private_dir)
            pdir.mkdir(parents=True)
            bad_content = "{not valid json!!!"
            (pdir / "state.json").write_text(bad_content, encoding="utf-8")
            with mock.patch.object(cchouse, "run_pipeline") as m_pipe, \
                    mock.patch.object(cchouse.subprocess, "run") as m_run:
                with self.assertRaises(SystemExit) as cm:
                    cchouse.cmd_up(args)
            self.assertIn("已损坏", str(cm.exception))
            self.assertEqual((pdir / "state.json").read_text(encoding="utf-8"), bad_content)
            m_pipe.assert_not_called()
            m_run.assert_not_called()


class TestUpRecoveryFlows(unittest.TestCase):
    def _write_state(self, pdir, **extra):
        state = {
            "tag": "cc01",
            "route": "tart-linux",
            "private_dir": str(pdir.resolve()),
            "vm_backend": "tart",
            "vm_name": "claude-cc01",
            "vm_created_by_up": True,
            "clean_snapshot": "claude-cc01-clean",
            "completed_steps": [f"up:{s}" for s, _ in cchouse.UP_STEPS],
            "share": {"enabled": True, "host_path": str((pdir.parent / "share").resolve()), "read_only": False},
        }
        state.update(extra)
        (pdir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        return state

    def test_both_absent_auto_rebuilds_without_prompt_and_resets_known_hosts(self):
        with tempfile.TemporaryDirectory() as td:
            exe = _make_fake_tart(td)
            th = Path(td) / "tart-home"
            th.mkdir()
            args = _up_args(td)
            pdir = Path(args.private_dir)
            share_dir = pdir.parent / "share"
            share_dir.mkdir(parents=True)
            _make_env(pdir)
            self._write_state(
                pdir, tart_bin=exe, tart_vm_identity=[1, 2], tart_vm_mac="02:00:00:00:00:01",
                tart_snapshot_identity=[1, 3], tart_snapshot_mac="02:00:00:00:00:02",
            )
            egress_ok = {"ip": "203.0.113.9", "country": "US", "asn": "AS64500", "timezone": "America/Los_Angeles"}
            captured_ctx = {}

            def fake_pipeline(ctx):
                captured_ctx["ctx"] = ctx
                return 0

            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"TART_HOME": str(th)}), \
                    mock.patch.object(cchouse.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="[]")), \
                    mock.patch.object(cchouse, "probe_egress_via_proxy", return_value=egress_ok) as m_probe, \
                    mock.patch.object(cchouse.getpass, "getpass", side_effect=AssertionError("should not prompt for node")), \
                    mock.patch("builtins.input", side_effect=AssertionError("should not ask confirmation")), \
                    mock.patch.object(cchouse, "run_pipeline", side_effect=fake_pipeline), \
                    redirect_stdout(buf):
                rc = cchouse.cmd_up(args)
            self.assertEqual(rc, 0)
            m_probe.assert_called_once()
            out = buf.getvalue()
            self.assertIn("旧虚拟机里的文件和登录状态找不回来", out)
            self.assertIn("共享文件夹里的文件不受影响", out)
            # known_hosts / guest_ip / hostkey files cleared
            self.assertFalse((pdir / "known_hosts").exists())
            self.assertFalse((pdir / "guest_ip.txt").exists())
            self.assertFalse((pdir / "logs" / "hostkey.serial").exists())
            self.assertFalse((pdir / "logs" / "hostkey.trust").exists())
            # config preserved, preflight..snapshot reset
            ctx = captured_ctx["ctx"]
            self.assertEqual(ctx.state["completed_steps"], ["up:config"])
            self.assertFalse(ctx.state["vm_created_by_up"])
            self.assertNotIn("tart_vm_identity", ctx.state)
            self.assertTrue(ctx.state["share"]["enabled"])

    def test_egress_drift_stops_and_hints_reconfigure_without_modifying_trust(self):
        with tempfile.TemporaryDirectory() as td:
            exe = _make_fake_tart(td)
            th = Path(td) / "tart-home"
            th.mkdir()
            args = _up_args(td)
            pdir = Path(args.private_dir)
            (pdir.parent / "share").mkdir(parents=True)
            _make_env(pdir)
            self._write_state(pdir, tart_bin=exe)
            drifted = {"ip": "198.51.100.77", "country": "GB", "asn": "AS64500", "timezone": "Europe/London"}
            with mock.patch.dict(os.environ, {"TART_HOME": str(th)}), \
                    mock.patch.object(cchouse.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="[]")), \
                    mock.patch.object(cchouse, "probe_egress_via_proxy", return_value=drifted), \
                    mock.patch.object(cchouse, "run_pipeline") as m_pipe, \
                    redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    cchouse.cmd_up(args)
            msg = str(cm.exception)
            self.assertIn("出口和上次记录不一致", msg)
            self.assertIn("--reconfigure", msg)
            self.assertTrue((pdir / "known_hosts").exists())
            m_pipe.assert_not_called()

    def test_mac_snapshot_only_clones_verified_snapshot_and_verifies_network(self):
        with tempfile.TemporaryDirectory() as td:
            exe = _make_fake_tart(td)
            th = Path(td) / "tart-home"
            snap_dir = th / "vms" / "claude-cc01-clean"
            snap_dir.mkdir(parents=True)
            st = snap_dir.lstat()
            snap_id = [st.st_dev, st.st_ino]
            args = _up_args(td)
            pdir = Path(args.private_dir)
            (pdir.parent / "share").mkdir(parents=True)
            _make_env(pdir)
            self._write_state(
                pdir, tart_bin=exe,
                tart_vm_identity=[999, 999], tart_vm_mac="02:00:00:00:00:01",
                tart_snapshot_identity=snap_id, tart_snapshot_mac="02:00:00:00:00:02",
            )
            inv = json.dumps([{"Name": "claude-cc01-clean", "State": "stopped"}])
            tart_ops = []

            def fake_tart_run(self_ctx, *op):
                tart_ops.append(op)
                return subprocess.CompletedProcess([], 0, "", "")

            captured_ctx = {}

            def fake_pipeline(ctx):
                captured_ctx["ctx"] = ctx
                return 0

            def fake_mac(name, _bin=None):
                return "02:00:00:00:00:02" if name == "claude-cc01-clean" else "02:00:00:00:00:09"

            with mock.patch.dict(os.environ, {"TART_HOME": str(th)}), \
                    mock.patch.object(cchouse.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=inv)), \
                    mock.patch.object(cchouse, "_tart_vm_mac", side_effect=fake_mac), \
                    mock.patch.object(cchouse.UpContext, "tart", fake_tart_run), \
                    mock.patch.object(cchouse, "run_pipeline", side_effect=fake_pipeline), \
                    redirect_stdout(io.StringIO()):
                rc = cchouse.cmd_up(args)
            self.assertEqual(rc, 0)
            self.assertEqual(tart_ops, [("clone-new", "claude-cc01-clean", "claude-cc01")])
            ctx = captured_ctx["ctx"]
            # boot, verify, snapshot must run after snapshot clone
            self.assertIn("up:firstboot", ctx.state["completed_steps"])
            self.assertNotIn("up:boot", ctx.state["completed_steps"])
            self.assertNotIn("up:verify", ctx.state["completed_steps"])
            self.assertNotIn("up:snapshot", ctx.state["completed_steps"])
            self.assertEqual(ctx.state["tart_vm_mac"], "02:00:00:00:00:09")

    def test_foreign_same_name_vm_zero_write_ops(self):
        with tempfile.TemporaryDirectory() as td:
            exe = _make_fake_tart(td)
            th = Path(td) / "tart-home"
            vm_dir = th / "vms" / "claude-cc01"
            vm_dir.mkdir(parents=True)
            args = _up_args(td)
            pdir = Path(args.private_dir)
            (pdir.parent / "share").mkdir(parents=True)
            _make_env(pdir)
            # Recorded identity does NOT match the existing VM on disk
            self._write_state(pdir, tart_bin=exe, tart_vm_identity=[111, 222], tart_vm_mac="02:00:00:00:00:01")
            state_before = (pdir / "state.json").read_text(encoding="utf-8")
            inv = json.dumps([{"Name": "claude-cc01", "State": "stopped"}])
            with mock.patch.dict(os.environ, {"TART_HOME": str(th)}), \
                    mock.patch.object(cchouse.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=inv)) as m_sub, \
                    mock.patch.object(cchouse.UpContext, "tart") as m_tart, \
                    mock.patch.object(cchouse, "run_pipeline") as m_pipe:
                with self.assertRaises(SystemExit) as cm:
                    cchouse.cmd_up(args)
            self.assertIn("无法确认归属", str(cm.exception))
            self.assertEqual((pdir / "state.json").read_text(encoding="utf-8"), state_before)
            m_tart.assert_not_called()
            m_pipe.assert_not_called()
            # Only read-only list was called (0 delete/clone/run)
            self.assertEqual(m_sub.call_count, 1)
            self.assertEqual(m_sub.call_args.args[0][1:], ["list", "--source", "local", "--format", "json"])

    def test_windows_foreign_same_name_vm_zero_write_ops(self):
        with tempfile.TemporaryDirectory() as td:
            args = _up_args(td, route="virtualbox-linux", backend="virtualbox")
            pdir = Path(args.private_dir)
            (pdir.parent / "share").mkdir(parents=True)
            _make_env(pdir)
            self._write_state(pdir, route="virtualbox-linux", vm_backend="virtualbox", clean_snapshot="cchouse-clean")
            state_before = (pdir / "state.json").read_text(encoding="utf-8")
            foreign_cfg = r"C:\Users\other\VirtualBox VMs\claude-cc01\claude-cc01.vbox"
            status_out = f"VM_STATE=stopped\nSNAPSHOT_STATE=missing\nCFG_FILE={foreign_cfg}\n"
            with mock.patch.object(cchouse.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=status_out)) as m_sub, \
                    mock.patch.object(cchouse.UpContext, "virtualbox") as m_vb, \
                    mock.patch.object(cchouse, "run_pipeline") as m_pipe:
                with self.assertRaises(SystemExit) as cm:
                    cchouse.cmd_up(args)
            self.assertIn("无法确认它属于这份安装记录", str(cm.exception))
            self.assertEqual((pdir / "state.json").read_text(encoding="utf-8"), state_before)
            m_vb.assert_not_called()
            m_pipe.assert_not_called()
            self.assertEqual(m_sub.call_count, 1)

    def test_legacy_record_without_ownership_fields_absent_can_rebuild(self):
        with tempfile.TemporaryDirectory() as td:
            exe = _make_fake_tart(td)
            th = Path(td) / "tart-home"
            th.mkdir()
            args = _up_args(td)
            pdir = Path(args.private_dir)
            (pdir.parent / "share").mkdir(parents=True)
            _make_env(pdir)
            # Legacy state: NO tart_vm_identity, NO tart_vm_mac, NO tart_snapshot_identity
            self._write_state(pdir, tart_bin=exe)
            egress_ok = {"ip": "203.0.113.9", "country": "US", "asn": "AS64500", "timezone": "America/Los_Angeles"}
            with mock.patch.dict(os.environ, {"TART_HOME": str(th)}), \
                    mock.patch.object(cchouse.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="[]")), \
                    mock.patch.object(cchouse, "probe_egress_via_proxy", return_value=egress_ok), \
                    mock.patch.object(cchouse, "run_pipeline", return_value=0) as m_pipe, \
                    redirect_stdout(io.StringIO()):
                rc = cchouse.cmd_up(args)
            self.assertEqual(rc, 0)
            m_pipe.assert_called_once()

    def test_legacy_record_without_ownership_fields_present_stops_zero_writes(self):
        with tempfile.TemporaryDirectory() as td:
            exe = _make_fake_tart(td)
            th = Path(td) / "tart-home"
            (th / "vms" / "claude-cc01").mkdir(parents=True)
            args = _up_args(td)
            pdir = Path(args.private_dir)
            (pdir.parent / "share").mkdir(parents=True)
            _make_env(pdir)
            # Legacy state without tart_vm_identity / tart_vm_mac
            self._write_state(pdir, tart_bin=exe)
            inv = json.dumps([{"Name": "claude-cc01", "State": "stopped"}])
            with mock.patch.dict(os.environ, {"TART_HOME": str(th)}), \
                    mock.patch.object(cchouse.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=inv)), \
                    mock.patch.object(cchouse.UpContext, "tart") as m_tart, \
                    mock.patch.object(cchouse, "run_pipeline") as m_pipe:
                with self.assertRaises(SystemExit) as cm:
                    cchouse.cmd_up(args)
            self.assertIn("无法核验", str(cm.exception))
            m_tart.assert_not_called()
            m_pipe.assert_not_called()

    def test_open_and_status_missing_instance_hint_rerun_home_cmd(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "privroot"
            pdir = root / "cc01"
            _make_env(pdir)
            self._write_state(pdir)
            with mock.patch.object(cchouse, "default_private_root", return_value=root), \
                    mock.patch.object(cchouse, "_backend_status", return_value=("missing", False)):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc_status = cchouse.cmd_status(argparse.Namespace(tag="cc01", private_dir=None, json=False))
                self.assertEqual(rc_status, 0)
                self.assertIn("重跑首页安装命令即可用保存的设置重建", out.getvalue())

                err = io.StringIO()
                with redirect_stderr(err):
                    rc_open = cchouse.cmd_open(argparse.Namespace(tag="cc01", private_dir=None))
                self.assertNotEqual(rc_open, 0)
                self.assertIn("重跑首页安装命令即可用保存的设置重建", err.getvalue())


if __name__ == "__main__":
    unittest.main()
