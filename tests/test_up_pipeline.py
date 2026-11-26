import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import cchouse as cchouse_mod  # noqa: E402
import downloads  # noqa: E402

PAYLOAD = os.urandom(3 * downloads.CHUNK + 12345)
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
PAYLOAD_SRI = "sha512-" + base64.b64encode(hashlib.sha512(PAYLOAD).digest()).decode("ascii")


class FakeResp:
    def __init__(self, body, status, fail_after=None, truncate_to=None):
        self._buf = io.BytesIO(body if truncate_to is None else body[:truncate_to])
        self.status = status
        self.headers = {"Content-Length": str(len(body))}  # 声明完整长度，实际可能提前 EOF
        self._reads = 0
        self._fail_after = fail_after

    def read(self, n):
        if self._fail_after is not None and self._reads >= self._fail_after:
            raise ConnectionResetError("simulated drop")
        self._reads += 1
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeServer:
    """记录每次请求的 Range 起点；可模拟中途断线、忽略 Range、返回坏数据。"""

    def __init__(self, body=PAYLOAD, honor_range=True, fail_after=None, eof_after=None, eof_times=1):
        self.body = body
        self.honor_range = honor_range
        self.fail_after = fail_after
        self.eof_after = eof_after  # 每次响应只给这么多字节就正常关闭（模拟代理提前 EOF）
        self.eof_times = eof_times
        self.calls = []

    def __call__(self, url, start, timeout):
        self.calls.append((url, start))
        fail_after, self.fail_after = self.fail_after, None  # 只断一次
        trunc = None
        if self.eof_after is not None and self.eof_times > 0:
            self.eof_times -= 1
            trunc = self.eof_after
        if start and self.honor_range:
            return FakeResp(self.body[start:], 206, fail_after, trunc)
        return FakeResp(self.body, 200, fail_after, trunc)


class TestDownloads(unittest.TestCase):
    def test_resume_after_drop_then_skip_when_verified(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "img.tvm"
            srv = FakeServer(fail_after=1)
            with self.assertRaises(downloads.DownloadError):
                downloads.fetch_verified(
                    ["https://x/img.tvm"], dest, sha256=PAYLOAD_SHA256, size=len(PAYLOAD), opener=srv, retries=0
                )
            part = Path(str(dest) + ".part")
            self.assertEqual(part.stat().st_size, downloads.CHUNK)
            self.assertFalse(dest.exists())

            prog = downloads.Progress("img")
            res = downloads.fetch_verified(
                ["https://x/img.tvm"], dest, sha256=PAYLOAD_SHA256, size=len(PAYLOAD), progress=prog, opener=srv
            )
            self.assertEqual(res, "downloaded")
            self.assertEqual(srv.calls[-1][1], downloads.CHUNK)  # 续传从断点开始
            self.assertEqual(dest.read_bytes(), PAYLOAD)
            self.assertFalse(part.exists())
            self.assertEqual(prog.start, downloads.CHUNK)
            self.assertEqual(prog.snapshot()["state"], "ok")

            n = len(srv.calls)
            self.assertEqual(downloads.fetch_verified(["https://x/img.tvm"], dest, sha256=PAYLOAD_SHA256, opener=srv), "skipped")
            self.assertEqual(len(srv.calls), n)

    def test_server_without_range_restarts_from_zero(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "a.bin"
            Path(str(dest) + ".part").write_bytes(PAYLOAD[:1000])
            srv = FakeServer(honor_range=False)
            logs = []
            downloads.fetch_verified(["u"], dest, sha256=PAYLOAD_SHA256, opener=srv, log=logs.append)
            self.assertEqual(dest.read_bytes(), PAYLOAD)
            self.assertTrue(any("不支持断点续传" in m for m in logs))

    def test_clean_eof_midway_resumes_in_same_run(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "img.tvm"
            cut = downloads.CHUNK + 777
            srv = FakeServer(eof_after=cut, eof_times=2)  # 连续两次提前 EOF，第三次正常
            logs = []
            with mock.patch.object(downloads.time, "sleep") as slp:
                res = downloads.fetch_verified(
                    ["u"], dest, sha256=PAYLOAD_SHA256, size=len(PAYLOAD), opener=srv, log=logs.append
                )
            self.assertEqual(res, "downloaded")
            self.assertEqual(dest.read_bytes(), PAYLOAD)
            self.assertEqual([c[1] for c in srv.calls], [0, cut, 2 * cut])  # 每次都从断点 Range 续传
            self.assertEqual([c.args[0] for c in slp.call_args_list], [2.0, 4.0])  # 指数退避
            self.assertTrue(logs[0].startswith("img.tvm 第 1/5 次续传"))
            self.assertIn("第 2/5 次续传", logs[1])

    def test_truncated_download_never_verified_or_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "img.tvm"
            srv = FakeServer(honor_range=False, eof_after=1000, eof_times=99)
            with mock.patch.object(downloads.time, "sleep") as slp, \
                    mock.patch.object(downloads, "_digest_matches", wraps=downloads._digest_matches) as dig:
                with self.assertRaises(downloads.DownloadError) as cm:
                    downloads.fetch_verified(["u"], dest, sha256=PAYLOAD_SHA256, size=len(PAYLOAD), opener=srv)
            self.assertIn("连接提前结束", str(cm.exception))
            self.assertEqual(len(srv.calls), 6)  # 首次 + 5 次续传
            self.assertEqual([c.args[0] for c in slp.call_args_list], [2.0, 4.0, 8.0, 16.0, 32.0])
            dig.assert_not_called()  # 未下完不校验
            self.assertTrue(Path(str(dest) + ".part").is_file())  # 不删 .part
            self.assertFalse(dest.exists())

    def test_corrupt_download_is_deleted_and_falls_back_to_next_url(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "a.bin"
            bad = FakeServer(body=b"x" * len(PAYLOAD))
            with self.assertRaises(downloads.DownloadError) as cm:
                downloads.fetch_verified(["u"], dest, sha256=PAYLOAD_SHA256, size=len(PAYLOAD), opener=bad)
            self.assertIn("校验不通过", str(cm.exception))
            self.assertFalse(Path(str(dest) + ".part").exists())  # 大小完整但 SHA 不符才删
            self.assertFalse(dest.exists())

            good = FakeServer()

            def opener(url, start, timeout):
                return (bad if url == "mirror" else good)(url, start, timeout)

            self.assertEqual(downloads.fetch_verified(["mirror", "official"], dest, sha256=PAYLOAD_SHA256, opener=opener), "downloaded")
            self.assertEqual(dest.read_bytes(), PAYLOAD)

    def test_tampered_file_after_marker_is_rehashed(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "a.bin"
            downloads.fetch_verified(["u"], dest, sha256=PAYLOAD_SHA256, opener=FakeServer())
            dest.write_bytes(b"tampered")
            self.assertFalse(downloads.is_verified(dest, sha256=PAYLOAD_SHA256))

    def test_mirror_urls_from_arg_and_env(self):
        art = {"filename": "img.ova", "urls": ["https://github.com/r/img.ova"]}
        with mock.patch.dict(os.environ, {downloads.IMAGE_MIRROR_ENV: ""}):
            self.assertEqual(downloads.image_urls(art), ["https://github.com/r/img.ova"])
            self.assertEqual(downloads.image_urls(art, "https://m.example/cc/"), ["https://m.example/cc/img.ova", "https://github.com/r/img.ova"])
        with mock.patch.dict(os.environ, {downloads.IMAGE_MIRROR_ENV: "https://env.example"}):
            self.assertEqual(downloads.image_urls(art)[0], "https://env.example/img.ova")
        tb = "https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-1.0.0.tgz"
        with mock.patch.dict(os.environ, {downloads.NPM_MIRROR_ENV: "https://registry.npmmirror.com"}):
            urls = downloads.claude_tarball_urls(tb)
        self.assertEqual(urls[0], "https://registry.npmmirror.com/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-1.0.0.tgz")
        self.assertEqual(urls[-1], tb)

    def test_progress_speed_and_slow_hint_once(self):
        prog = downloads.Progress("img")
        prog.update(state="downloading", start=0, done=0, total=100 * 1048576)
        prog.started = time.monotonic() - 11
        prog._samples = [(prog.started, 0)]
        prog.update(done=int(0.5 * 1048576 * 11))
        self.assertGreater(prog.speed(), 0)
        self.assertIn("MB/s", prog.render())
        slow = prog.slow_hint()
        self.assertIsNotNone(slow)
        self.assertLess(slow, downloads.SLOW_THRESHOLD_MB_S)
        self.assertIsNone(prog.slow_hint())  # 只提示一次
        self.assertIn("镜像源", downloads.slow_download_tip("img", slow))

    def test_fast_download_gives_no_slow_hint(self):
        prog = downloads.Progress("img")
        prog.update(state="downloading", start=0, done=0)
        prog.started = time.monotonic() - 11
        prog.update(done=50 * 1048576)
        self.assertIsNone(prog.slow_hint())

    def test_fetch_claude_code_sri_and_offline_cache(self):
        meta = {"version": "9.9.9", "dist": {"tarball": "https://registry.npmjs.org/x.tgz", "integrity": PAYLOAD_SRI}}
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "claude.tgz"
            res = downloads.fetch_claude_code("linux-x64", out, fetch=lambda url: meta, opener=FakeServer())
            self.assertEqual(res["status"], "downloaded")
            self.assertEqual(res["version"], "9.9.9")

            def offline(url):
                raise OSError("offline")

            res2 = downloads.fetch_claude_code("linux-x64", out, fetch=offline, opener=FakeServer())
            self.assertEqual(res2["status"], "skipped")

            bad_meta = {"version": "1", "dist": {"tarball": "https://registry.npmjs.org/y.tgz", "integrity": "sha512-AAAA"}}
            with self.assertRaises(downloads.DownloadError):
                downloads.fetch_claude_code("linux-x64", Path(td) / "other.tgz", fetch=lambda url: bad_meta, opener=FakeServer())


def _ctx(td, route="tart-linux", **over):
    args = types.SimpleNamespace(
        tag="cc05", route=route, private_dir=str(Path(td) / "cc05"), cache_dir=str(Path(td) / "cache"),
        dry_run=False, config_only=False, reconfigure=False, no_probe=True, link_stdin=True, non_interactive=True,
        timezone=None, expected_ip=None, image_mirror=None, npm_mirror=None,
    )
    for k, v in over.items():
        setattr(args, k, v)
    ctx = cchouse_mod.UpContext(args, "cc05", route, Path(args.private_dir))
    ctx.backend = "tart"
    ctx.start_downloads = lambda: None
    return ctx


class TestUpPipeline(unittest.TestCase):
    def _stub_steps(self, calls, fail=None, cached=()):
        def make(name):
            def fn(ctx):
                calls.append(name)
                if name == "seed":
                    ctx.private_dir.mkdir(parents=True, exist_ok=True)
                    ctx.seed_iso.write_bytes(b"plaintext")
                    ctx.rendered.mkdir(exist_ok=True)
                if name == fail:
                    raise cchouse_mod.StepError(f"{name} boom")
                return name in cached
            return fn
        return {name: make(name) for name, _t in cchouse_mod.UP_STEPS}

    def test_prompt_eof_uses_default_and_explains_it(self):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO()), mock.patch("sys.stdout", out):
            value = cchouse_mod._prompt_with_default("出口时区", "America/Los_Angeles")
        self.assertEqual(value, "America/Los_Angeles")
        self.assertIn("输入已结束，使用默认值 [America/Los_Angeles]", out.getvalue())

    def test_required_input_eof_fails_and_cleans_plaintext(self):
        for link_stdin, stdin, hint in (
            (True, "socks5://u:p@203.0.113.9:1080\n", "--non-interactive"),
            (True, "", "--link-stdin"),
            (False, "", "--link-stdin"),
        ):
            with self.subTest(link_stdin=link_stdin, hint=hint), tempfile.TemporaryDirectory() as td:
                ctx = _ctx(td, link_stdin=link_stdin, non_interactive=False)
                ctx.rendered.mkdir(parents=True)
                (ctx.rendered / "config").write_text("plaintext", encoding="utf-8")
                ctx.seed_iso.write_bytes(b"plaintext")
                out = io.StringIO()
                with mock.patch("sys.stdin", io.StringIO(stdin)), mock.patch("sys.stdout", out), \
                        mock.patch.object(cchouse_mod.getpass, "getpass", side_effect=EOFError):
                    rc = cchouse_mod.run_pipeline(ctx)
                self.assertEqual(rc, 1)
                self.assertIn("输入已结束，请在终端交互运行", out.getvalue())
                self.assertIn(hint, out.getvalue())
                self.assertNotIn("traceback", out.getvalue().lower())
                self.assertFalse(ctx.env_path.exists())
                self.assertFalse(ctx.seed_iso.exists())
                self.assertFalse(ctx.rendered.exists())
                state = json.loads(ctx.state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["failed_step"], "up:config")
                self.assertEqual(state["status"], "FAILED")

    def test_failure_stops_records_state_and_resume_continues(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []
            ctx = _ctx(td)
            out = io.StringIO()
            with mock.patch.dict(cchouse_mod.STEP_FUNCS, self._stub_steps(calls, fail="import", cached=("download",))), \
                    mock.patch("sys.stdout", out):
                rc = cchouse_mod.run_pipeline(ctx)
            self.assertEqual(rc, 1)
            self.assertEqual(calls, ["config", "preflight", "seed", "download", "import"])
            state = json.loads(ctx.state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "FAILED")
            self.assertEqual(state["failed_step"], "up:import")
            self.assertIn("up:download", state["completed_steps"])
            self.assertNotIn("up:import", state["completed_steps"])
            self.assertEqual(state["timings"]["import"]["result"], "failed")
            self.assertEqual(state["timings"]["download"]["result"], "cached")
            for t in state["timings"].values():
                self.assertIn("started_at", t)
                self.assertIn("ended_at", t)
                self.assertIn("seconds", t)
            # 失败即删明文
            self.assertFalse(ctx.seed_iso.exists())
            self.assertFalse(ctx.rendered.exists())
            text = out.getvalue()
            self.assertIn("导入/克隆黄金镜像", text)
            self.assertIn("会从这一步继续", text)
            self.assertIn("scripts/doctor.py", text)
            self.assertIn("耗时表", text)
            if os.name != "nt":
                self.assertEqual(os.stat(ctx.state_path).st_mode & 0o777, 0o600)

            # 断点续跑：已完成步骤跳过；seed.iso 被删了，首启前需重新生成
            calls2 = []
            ctx2 = _ctx(td)
            out2 = io.StringIO()
            with mock.patch.dict(cchouse_mod.STEP_FUNCS, self._stub_steps(calls2)), mock.patch("sys.stdout", out2):
                rc2 = cchouse_mod.run_pipeline(ctx2)
            self.assertEqual(rc2, 0)
            self.assertEqual(calls2, ["seed", "import", "firstboot", "boot", "verify", "snapshot"])
            state2 = json.loads(ctx2.state_path.read_text(encoding="utf-8"))
            self.assertEqual(state2["status"], "DONE")
            self.assertNotIn("failed_step", state2)
            self.assertEqual(len(state2["timings"]), len(cchouse_mod.UP_STEPS))
            self.assertIn("check.html", out2.getvalue())
            self.assertIn("严禁复制到宿主浏览器", out2.getvalue())

    def test_unexpected_error_deletes_plaintext_marks_failed_and_reraises(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            steps = self._stub_steps([])

            def boom(c):
                raise RuntimeError("unexpected")

            steps["download"] = boom
            with mock.patch.dict(cchouse_mod.STEP_FUNCS, steps), mock.patch("sys.stdout", io.StringIO()):
                with self.assertRaises(RuntimeError):
                    cchouse_mod.run_pipeline(ctx)
            self.assertFalse(ctx.seed_iso.exists())
            self.assertFalse(ctx.rendered.exists())
            state = json.loads(ctx.state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "FAILED")
            self.assertEqual(state["failed_step"], "up:download")
            self.assertEqual(state["timings"]["download"]["result"], "failed")

    def test_firstboot_failure_forces_reimport(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []
            ctx = _ctx(td)
            with mock.patch.dict(cchouse_mod.STEP_FUNCS, self._stub_steps(calls, fail="firstboot")), \
                    mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(cchouse_mod.run_pipeline(ctx), 1)
            state = json.loads(ctx.state_path.read_text(encoding="utf-8"))
            self.assertNotIn("up:import", state["completed_steps"])
            calls2 = []
            with mock.patch.dict(cchouse_mod.STEP_FUNCS, self._stub_steps(calls2)), mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(cchouse_mod.run_pipeline(_ctx(td)), 0)
            self.assertEqual(calls2[:3], ["seed", "import", "firstboot"])

    def test_manual_backend_stops_after_download_with_human_wait(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []
            ctx = _ctx(td, route="vmware-linux")
            ctx.backend = "manual"
            out = io.StringIO()
            with mock.patch.dict(cchouse_mod.STEP_FUNCS, self._stub_steps(calls)), mock.patch("sys.stdout", out):
                self.assertEqual(cchouse_mod.run_pipeline(ctx), 0)
            self.assertEqual(calls, ["config", "preflight", "seed", "download"])
            state = json.loads(ctx.state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "HUMAN_WAIT")
            self.assertIn("verify-linux.sh", out.getvalue())

    def test_cold_vs_warm_classification(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            ctx.state["downloads"] = {"image": {"label": "img", "status": "downloaded", "bytes_total": 1, "seconds": 1, "network": "直连"}}
            ctx.state["timings"] = {"import": {"seconds": 3, "result": "fresh"}}
            ctx.downloader.items = {}
            with mock.patch("sys.stdout", io.StringIO()) as out:
                cchouse_mod.print_timing_table(ctx, cchouse_mod.UP_STEPS)
            self.assertEqual(ctx.state["run_kind"], "cold")
            self.assertIn("冷启动", out.getvalue())
            ctx.state["downloads"]["image"]["status"] = "skipped"
            with mock.patch("sys.stdout", io.StringIO()):
                cchouse_mod.print_timing_table(ctx, cchouse_mod.UP_STEPS)
            self.assertEqual(ctx.state["run_kind"], "warm")

    def test_import_refuses_to_overwrite_foreign_vm(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td, route="vmware-linux")
            ctx.backend = "vmware"
            ctx.vmx.parent.mkdir(parents=True)
            ctx.vmx.write_text("logged-in vm")
            ran = []
            ctx.run = lambda cmd, env=None: ran.append(cmd)
            with self.assertRaises(cchouse_mod.StepError) as cm:
                cchouse_mod.step_import(ctx)
            self.assertIn("不会删除", str(cm.exception))
            self.assertFalse(ctx.state.get("vm_created_by_up"))
            self.assertEqual(ran, [])
            self.assertEqual(ctx.vmx.read_text(), "logged-in vm")
            # up 自己建的（首启失败后重来）允许重建
            ctx.state["vm_created_by_up"] = True
            ctx.env_path.parent.mkdir(parents=True, exist_ok=True)
            cchouse_mod.write_env_secure(ctx.env_path, "TAG=cc05\n")
            cchouse_mod.step_import(ctx)
            self.assertTrue(any("-CcOp" in c and "import" in c for c in ran))
            state = json.loads(ctx.state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["vm_name"], "claude-cc05")
            self.assertEqual(state["vm_backend"], "vmware")
            self.assertEqual(state["private_dir"], str(ctx.private_dir.resolve()))

    def test_tart_import_refuses_existing_guest_without_claiming_it(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            with mock.patch.object(cchouse_mod.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                    self.assertRaisesRegex(cchouse_mod.StepError, "已存在同名虚拟机"):
                cchouse_mod.step_import(ctx)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][-2:], ["exists", ctx.vm_name])
            self.assertFalse(ctx.state.get("vm_created_by_up"))
            self.assertFalse(ctx.state_path.exists())

    def test_failed_fresh_clone_leaves_no_ownership_marker(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = _ctx(td)
            results = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]
            with mock.patch.object(cchouse_mod.subprocess, "run", side_effect=results), \
                    mock.patch("sys.stdout", io.StringIO()), \
                    mock.patch.object(ctx, "tart", side_effect=cchouse_mod.StepError("name already exists")) as tart, \
                    self.assertRaises(cchouse_mod.StepError):
                cchouse_mod.step_import(ctx)
            tart.assert_called_once_with("clone-new", ctx.golden_name, ctx.vm_name)
            self.assertFalse(ctx.state.get("vm_created_by_up"))
            self.assertFalse(ctx.state_path.exists())

    def test_update_env_value_keeps_600_and_other_keys(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / "instance.env"
            cchouse_mod.write_env_secure(env, "TAG=cc05\nGUEST_IP=192.168.64.2\nSINGLE_PASS='s3cr3t'\n")
            cchouse_mod.update_env_value(env, "GUEST_IP", "192.168.64.7")
            cchouse_mod.update_env_value(env, "VMX_PATH", '"C:/x y/a.vmx"')
            text = env.read_text(encoding="utf-8")
            self.assertIn("GUEST_IP=192.168.64.7\n", text)
            self.assertNotIn("192.168.64.2", text)
            self.assertIn("SINGLE_PASS='s3cr3t'", text)
            self.assertIn('VMX_PATH="C:/x y/a.vmx"', text)
            if os.name != "nt":
                self.assertEqual(os.stat(env).st_mode & 0o777, 0o600)

    def test_dry_run_end_to_end_never_leaks_and_writes_no_state(self):
        secret = "DryRunSecret!42"
        with tempfile.TemporaryDirectory() as td:
            priv = Path(td) / "cc06"
            cases = (
                ("tart-linux", "tart", "vm-tart.sh"),
                ("vmware-linux", "vmware", "vm-vmware.ps1"),
                ("vmware-linux", "virtualbox", "vm-virtualbox.ps1"),
            )
            for route, backend, marker in cases:
                proc = subprocess.run(
                    [sys.executable, "-I", str(ROOT / "scripts" / "cchouse.py"), "up", "--tag", "cc06",
                     "--route", route, "--vm-backend", backend, "--private-dir", str(priv), "--cache-dir", str(Path(td) / "cache"),
                     "--link-stdin", "--non-interactive", "--no-probe", "--expected-ip", "203.0.113.9",
                     "--reconfigure", "--dry-run", "--image-mirror", "https://mirror.example/cc"],
                    input=f"trojan://{secret}@203.0.113.9:443?sni=a.example#n\n",
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                    env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=60,
                )
                out = proc.stdout + proc.stderr
                self.assertEqual(proc.returncode, 0, out)
                self.assertNotIn(secret, out)
                self.assertNotIn("trojan://", out)
                self.assertIn("[9/9]", out)
                self.assertIn("耗时表", out)
                self.assertIn("https://mirror.example/cc/cchouse-debian13-", out)
                self.assertIn(marker, out)
                self.assertIn("verify-linux.sh", out)
                self.assertFalse((priv / "state.json").exists())
                self.assertFalse((priv / "seed.iso").exists())
                self.assertFalse((Path(td) / "cache").exists())
                env_text = (priv / "instance.env").read_text(encoding="utf-8")
                if backend == "virtualbox":
                    self.assertIn("download.virtualbox.org", out)
                    self.assertNotIn("Extension_Pack", out)
                    self.assertNotIn("MISSING_VMWARE", out)
                    self.assertNotIn("vm-vmware.ps1", out)
                    self.assertIn("VMNET_CIDR=10.0.2.0/24", env_text)
                    self.assertIn("GUEST_IP=127.0.0.1\n", env_text)
                    self.assertIn("GUEST_IF=enp0s3", env_text)
                    self.assertIn("SSH_PORT=2222", env_text)
                    self.assertIn("ROUTER_IP=192.168.100.1", env_text)
                else:
                    self.assertNotIn("SSH_PORT=", env_text)


if __name__ == "__main__":
    unittest.main()
