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
import share  # noqa: E402


def _make_state(private_dir, tag="cc01", backend="tart", vm_name="claude-cc01", share_cfg=None):
    private_dir.mkdir(parents=True, exist_ok=True)
    snap = f"{vm_name}-clean" if backend == "tart" else cchouse.CLEAN_SNAPSHOT
    state = {
        "tag": tag,
        "route": "tart-linux" if backend == "tart" else "virtualbox-linux",
        "private_dir": str(private_dir.resolve()),
        "cache_dir": str((private_dir.parent / "cache").resolve()),
        "vm_backend": backend,
        "vm_name": vm_name,
        "vm_created_by_up": True,
        "clean_snapshot": snap,
        "launchers": [],
    }
    if share_cfg is not None:
        state["share"] = share_cfg
    (private_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    (private_dir / "instance.env").write_text("GUEST_IP=192.168.64.2\nSSH_PORT=2222\n", encoding="utf-8")
    (private_dir / "id_ed25519").write_text("dummy-key\n", encoding="utf-8")
    return state


class TestSharePathValidation(unittest.TestCase):
    def test_posix_rejects_forbidden_paths(self):
        home = "/Users/alice"
        forbidden = [
            "/",
            "/Users",
            "/Users/",
            "/users",
            home,
            home + "/",
            "~",
            "~/",
            f"{home}/Desktop",
            f"{home}/desktop",
            f"{home}/Documents",
            f"{home}/Downloads",
            f"{home}/桌面",
            f"{home}/文档",
            f"{home}/下载",
            f"{home}/Desktop/",
            "/Users/alice/cchouse:bad",
        ]
        for path in forbidden:
            with self.subTest(path=path):
                with self.assertRaises(share.SharePathError):
                    share.validate_share_path(path, platform_name="Darwin", home=home, check_fs=False)

    def test_posix_allows_subdirectories_of_sensitive_dirs(self):
        home = "/Users/alice"
        allowed = [
            f"{home}/cchouse-share/cc01",
            f"{home}/Desktop/work-share",
            f"{home}/Documents/claude-files",
            f"{home}/Downloads/inbox",
            f"{home}/桌面/项目",
            f"{home}/文档/资料",
            f"{home}/下载/中转",
        ]
        for path in allowed:
            with self.subTest(path=path):
                got = share.validate_share_path(path, platform_name="Darwin", home=home, check_fs=False)
                self.assertTrue(got.startswith(home + "/"))

    def test_windows_rejects_forbidden_paths_case_and_mixed_slashes(self):
        home = r"C:\Users\Alice"
        forbidden = [
            r"C:\\",
            "C:/",
            "c:\\",
            "c:/",
            "D:\\",
            "d:/",
            r"C:\Users",
            "c:/users/",
            r"C:\USERS",
            r"C:\Users\Alice",
            "c:/users/alice/",
            r"C:\Users\alice\Desktop",
            "c:/Users/Alice/desktop/",
            r"C:\Users\Alice\DOCUMENTS",
            "c:/users/alice/Downloads",
            r"C:\Users\Alice\桌面",
            "c:/Users/Alice/文档",
            r"C:\Users\Alice\下载",
            r"\\?\C:\Users\Alice\Desktop",
            r"\\server\share",
        ]
        for path in forbidden:
            with self.subTest(path=path):
                with self.assertRaises(share.SharePathError):
                    share.validate_share_path(path, platform_name="Windows", home=home, check_fs=False)

    def test_windows_allows_subdirectories_of_sensitive_dirs(self):
        home = r"C:\Users\Alice"
        allowed = [
            r"C:\Users\Alice\cchouse-share\cc01",
            "c:/users/alice/Desktop/work",
            r"C:\Users\Alice\Documents\project",
            "c:/Users/Alice/Downloads/sub",
            r"C:\Users\Alice\桌面\工作",
            r"D:\shared-work",
        ]
        for path in allowed:
            with self.subTest(path=path):
                got = share.validate_share_path(path, platform_name="Windows", home=home, check_fs=False)
                self.assertTrue(len(got) > 3)

    def test_nonexistent_and_file_rejected_unless_allow_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "fakehome"
            home.mkdir()
            missing = home / "does-not-exist"
            with self.assertRaises(share.SharePathError):
                share.validate_share_path(str(missing), home=str(home), allow_create=False)

            # 默认目录 allow_create=True 允许尚不存在
            default_dir = share.default_share_dir("cc01", home=str(home))
            got = share.validate_share_path(default_dir, home=str(home), allow_create=True)
            self.assertTrue(got.endswith(os.path.join("cchouse-share", "cc01")))

            # 普通文件必须拒绝
            f = home / "plain.txt"
            f.write_text("hi", encoding="utf-8")
            with self.assertRaises(share.SharePathError):
                share.validate_share_path(str(f), home=str(home), allow_create=False)

    @unittest.skipIf(os.name == "nt", "POSIX symlink test")
    def test_symlinks_to_forbidden_targets_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "fakehome"
            home.mkdir()
            desktop = home / "Desktop"
            desktop.mkdir()
            allowed_sub = desktop / "project"
            allowed_sub.mkdir()
            links = Path(tmp) / "links"
            links.mkdir()

            link_home = links / "to-home"
            link_home.symlink_to(home)
            link_desktop = links / "to-desktop"
            link_desktop.symlink_to(desktop)
            link_root = links / "to-root"
            link_root.symlink_to("/")
            link_ok = links / "to-project"
            link_ok.symlink_to(allowed_sub)

            for bad_link in (link_home, link_desktop, link_root):
                with self.subTest(link=str(bad_link)):
                    with self.assertRaises(share.SharePathError):
                        share.validate_share_path(str(bad_link), home=str(home), allow_create=False)

            got = share.validate_share_path(str(link_ok), home=str(home), allow_create=False)
            self.assertEqual(os.path.realpath(got), os.path.realpath(str(allowed_sub)))


class TestShareCliAndLifecycle(unittest.TestCase):
    def test_share_show_set_ro_off_and_running_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "privroot"
            pdir = root / "cc01"
            _make_state(pdir, backend="tart")
            share_dir = Path(tmp) / "my-share"
            share_dir.mkdir()

            with mock.patch.object(cchouse, "default_private_root", return_value=root), \
                 mock.patch.object(cchouse, "_backend_status", return_value=("running", True)), \
                 mock.patch.object(cchouse, "_guest_has_share_service", return_value=True):
                # 无参数显示未开启
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cchouse.cmd_share(argparse.Namespace(tag="cc01", private_dir=None, path=None, read_only=False, off=False))
                self.assertEqual(rc, 0)
                self.assertIn("共享文件夹：未开启", buf.getvalue())

                # 运行中设置只读共享 -> 提示重启后生效
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cchouse.cmd_share(argparse.Namespace(tag="cc01", private_dir=None, path=str(share_dir), read_only=True, off=False))
                self.assertEqual(rc, 0)
                out = buf.getvalue()
                self.assertIn("只读", out)
                self.assertIn("重启后生效", out)

                state = json.loads((pdir / "state.json").read_text(encoding="utf-8"))
                self.assertTrue(state["share"]["enabled"])
                self.assertTrue(state["share"]["read_only"])
                self.assertEqual(os.path.realpath(state["share"]["host_path"]), os.path.realpath(str(share_dir)))

                # status 显示共享文件夹（只读）
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cchouse.cmd_status(argparse.Namespace(tag="cc01", private_dir=None, json=False))
                self.assertEqual(rc, 0)
                self.assertIn("共享文件夹：", buf.getvalue())
                self.assertIn("（只读）", buf.getvalue())

                # 关闭共享
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cchouse.cmd_share(argparse.Namespace(tag="cc01", private_dir=None, path=None, read_only=False, off=True))
                self.assertEqual(rc, 0)
                self.assertIn("重启后生效", buf.getvalue())
                state = json.loads((pdir / "state.json").read_text(encoding="utf-8"))
                self.assertFalse(state["share"]["enabled"])

    def test_share_stopped_virtualbox_syncs_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "privroot"
            pdir = root / "cc02"
            _make_state(pdir, tag="cc02", backend="virtualbox", vm_name="claude-cc02")
            share_dir = Path(tmp) / "win-share"
            share_dir.mkdir()

            with mock.patch.object(cchouse, "default_private_root", return_value=root), \
                 mock.patch.object(cchouse, "_backend_status", return_value=("stopped", True)), \
                 mock.patch.object(cchouse.subprocess, "run") as m_run:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cchouse.cmd_share(argparse.Namespace(tag="cc02", private_dir=None, path=str(share_dir), read_only=True, off=False))
                self.assertEqual(rc, 0)
                cmd = m_run.call_args[0][0]
                self.assertEqual(cmd[cmd.index("-CcOp") + 1], "sync-share")
                self.assertEqual(cmd[cmd.index("-CcShareEnabled") + 1], "yes")
                self.assertEqual(cmd[cmd.index("-CcShareReadOnly") + 1], "yes")

    def test_restore_preserves_share_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "privroot"
            pdir = root / "cc01"
            share_dir = Path(tmp) / "keep-share"
            share_dir.mkdir()
            share_cfg = share.make_share_state(str(share_dir), read_only=True, enabled=True)
            _make_state(pdir, backend="virtualbox", vm_name="claude-cc01", share_cfg=share_cfg)

            with mock.patch.object(cchouse, "default_private_root", return_value=root), \
                 mock.patch.object(cchouse, "_backend_status", return_value=("stopped", True)), \
                 mock.patch.object(cchouse.subprocess, "run") as m_run:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cchouse.cmd_restore(argparse.Namespace(tag="cc01", private_dir=None, yes=True))
                self.assertEqual(rc, 0)
                cmd = m_run.call_args[0][0]
                self.assertEqual(cmd[cmd.index("-CcOp") + 1], "restore")
                self.assertEqual(cmd[cmd.index("-CcShareEnabled") + 1], "yes")
                self.assertEqual(cmd[cmd.index("-CcShareReadOnly") + 1], "yes")

            state = json.loads((pdir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["share"], share_cfg)

    def test_remove_preserves_host_share_folder_and_prints_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "privroot"
            pdir = root / "cc01"
            share_dir = Path(tmp) / "user-share"
            share_dir.mkdir()
            keep_file = share_dir / "重要文档.txt"
            keep_file.write_text("do not delete", encoding="utf-8")

            share_cfg = share.make_share_state(str(share_dir), read_only=False, enabled=True)
            state = _make_state(pdir, backend="virtualbox", vm_name="claude-cc01", share_cfg=share_cfg)
            state["vm_created_by_up"] = False
            (pdir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

            with mock.patch.object(cchouse, "default_private_root", return_value=root):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cchouse.cmd_remove(argparse.Namespace(tag="cc01", private_dir=None, yes=True, purge_cache=False))
                self.assertEqual(rc, 0)
                out = buf.getvalue()
                self.assertIn(f"共享文件夹 {share_dir} 已保留", out)
            self.assertFalse(pdir.exists())
            self.assertTrue(share_dir.is_dir())
            self.assertEqual(keep_file.read_text(encoding="utf-8"), "do not delete")

    def test_backend_args_generation_tart_and_vbox(self):
        env_rw = cchouse._tart_env("/usr/local/bin/tart", share.make_share_state("/tmp/s1", read_only=False))
        self.assertEqual(env_rw["CCHOUSE_SHARE_PATH"], "/tmp/s1")
        self.assertEqual(env_rw["CCHOUSE_SHARE_RO"], "0")

        env_ro = cchouse._tart_env("/usr/local/bin/tart", share.make_share_state("/tmp/s1", read_only=True))
        self.assertEqual(env_ro["CCHOUSE_SHARE_RO"], "1")

        env_off = cchouse._tart_env("/usr/local/bin/tart", share.make_share_state(enabled=False))
        self.assertNotIn("CCHOUSE_SHARE_PATH", env_off)

        vb_ro = cchouse._vbox_share_kwargs({"share": share.make_share_state(r"C:\s1", read_only=True)})
        self.assertEqual(vb_ro, {"ShareEnabled": "yes", "SharePath": r"C:\s1", "ShareReadOnly": "yes"})
        vb_off = cchouse._vbox_share_kwargs({"share": share.make_share_state(enabled=False)})
        self.assertEqual(vb_off, {"ShareEnabled": "no", "SharePath": "", "ShareReadOnly": "no"})

    @unittest.skipIf(os.name == "nt", "bash script test")
    def test_vm_tart_dir_args_bash(self):
        script = f"""
source "{ROOT / 'scripts' / 'vm-tart.sh'}"
CCHOUSE_SHARE_PATH="/tmp/my dir" CCHOUSE_SHARE_RO=0 cchouse_tart_dir_args
printf 'RW=%s\\n' "${{CCHOUSE_TART_DIR_ARGS[*]}}"
CCHOUSE_SHARE_PATH="/tmp/my dir" CCHOUSE_SHARE_RO=1 cchouse_tart_dir_args
printf 'RO=%s\\n' "${{CCHOUSE_TART_DIR_ARGS[*]}}"
unset CCHOUSE_SHARE_PATH CCHOUSE_SHARE_RO
cchouse_tart_dir_args
printf 'OFF=%d\\n' "${{#CCHOUSE_TART_DIR_ARGS[@]}}"
"""
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
        self.assertIn("RW=--dir=shared:/tmp/my dir", proc.stdout)
        self.assertIn("RO=--dir=shared:/tmp/my dir:ro", proc.stdout)
        self.assertIn("OFF=0", proc.stdout)


class TestShareVerifyChecks(unittest.TestCase):
    def test_vbox_showvminfo_share_pass_and_fail(self):
        cfg_on = share.make_share_state(r"C:\Users\Alice\cchouse-share\cc01", read_only=False)
        cfg_off = share.make_share_state(enabled=False)

        ok, _ = share.check_vbox_showvminfo_share([
            'draganddrop="disabled"',
            'SharedFolderNameMachineMapping1="cchouse-shared"',
            r'SharedFolderPathMachineMapping1="C:\\Users\\Alice\\cchouse-share\\cc01"',
        ], cfg_on)
        self.assertTrue(ok)

        # 拖放未关闭 -> FAIL
        ok, msg = share.check_vbox_showvminfo_share([
            'draganddrop="bidirectional"',
            'SharedFolderNameMachineMapping1="cchouse-shared"',
            r'SharedFolderPathMachineMapping1="C:\\Users\\Alice\\cchouse-share\\cc01"',
        ], cfg_on)
        self.assertFalse(ok)
        self.assertIn("拖放", msg)

        # 多余共享目录 -> FAIL
        ok, msg = share.check_vbox_showvminfo_share([
            'draganddrop="disabled"',
            'SharedFolderNameMachineMapping1="cchouse-shared"',
            r'SharedFolderPathMachineMapping1="C:\\Users\\Alice\\cchouse-share\\cc01"',
            'SharedFolderNameMachineMapping2="extra"',
            r'SharedFolderPathMachineMapping2="C:\\extra"',
        ], cfg_on)
        self.assertFalse(ok)
        self.assertIn("2", msg)

        # 路径不符 -> FAIL
        ok, msg = share.check_vbox_showvminfo_share([
            'draganddrop="disabled"',
            'SharedFolderNameMachineMapping1="cchouse-shared"',
            r'SharedFolderPathMachineMapping1="C:\\Other\\Dir"',
        ], cfg_on)
        self.assertFalse(ok)
        self.assertIn("路径不符", msg)

        # 未开启共享：0 个映射 PASS，有映射 FAIL
        ok, _ = share.check_vbox_showvminfo_share(['draganddrop="disabled"'], cfg_off)
        self.assertTrue(ok)
        ok, _ = share.check_vbox_showvminfo_share([
            'draganddrop="disabled"',
            'SharedFolderNameMachineMapping1="cchouse-shared"',
            r'SharedFolderPathMachineMapping1="C:\\Users\\Alice\\cchouse-share\\cc01"',
        ], cfg_off)
        self.assertFalse(ok)

    @unittest.skipIf(os.name == "nt", "bash awk verifier logic test")
    def test_verify_mounts_awk_logic_pass_and_fail(self):
        # 提取并运行 verify-linux.sh 的挂载审计逻辑，覆盖通过与多种失败场景
        awk_prog = r"""
$3 == "virtiofs" || $3 == "vboxsf" || $3 == "vmhgfs" || $3 == "fuse.vmhgfs-fuse" || $3 == "9p" {
  total++
  if (en == "0") {
    extra = extra (extra ? ", " : "") $2 "(" $3 ")"
  } else if ($3 == "virtiofs") {
    if ($1 == "com.apple.virtio-fs.automount" && ($2 == gp || $2 == "/run/cchouse-virtiofs")) {
      if ($2 == gp) { mounted = "virtiofs" }
    } else {
      extra = extra (extra ? ", " : "") $2 "(" $3 ":" $1 ")"
    }
  } else if ($3 == "vboxsf") {
    if ($1 == "cchouse-shared" && $2 == gp) {
      mounted = "vboxsf"
    } else {
      extra = extra (extra ? ", " : "") $2 "(" $3 ":" $1 ")"
    }
  } else {
    extra = extra (extra ? ", " : "") $2 "(" $3 ")"
  }
}
END {
  if (en == "0") {
    if (total == 0) print "ok:off"
    else print "fail:extra:" extra
  } else {
    if (extra != "") print "fail:extra:" extra
    else if (mounted == "") print "fail:missing"
    else print "ok:" mounted
  }
}
"""
        cases = [
            ("0", "", "ok:off"),
            ("0", "cchouse-shared /home/claude/shared vboxsf rw 0 0\n", "fail:extra:/home/claude/shared(vboxsf)"),
            ("1", "com.apple.virtio-fs.automount /run/cchouse-virtiofs virtiofs rw 0 0\ncom.apple.virtio-fs.automount /home/claude/shared virtiofs rw 0 0\n", "ok:virtiofs"),
            ("1", "cchouse-shared /home/claude/shared vboxsf rw 0 0\n", "ok:vboxsf"),
            ("1", "other /mnt/other vboxsf rw 0 0\n", "fail:extra:/mnt/other(vboxsf:other)"),
            ("1", "cchouse-shared /home/claude/shared vboxsf rw 0 0\nvmhgfs /mnt/hgfs fuse.vmhgfs-fuse rw 0 0\n", "fail:extra:/mnt/hgfs(fuse.vmhgfs-fuse)"),
            ("1", "/dev/sda1 / ext4 rw 0 0\n", "fail:missing"),
        ]
        for en, mounts, expected in cases:
            with self.subTest(en=en, mounts=mounts.strip()):
                proc = subprocess.run(
                    ["awk", "-v", f"en={en}", "-v", "gp=/home/claude/shared", awk_prog],
                    input=mounts, capture_output=True, text=True, check=True,
                )
                self.assertEqual(proc.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
