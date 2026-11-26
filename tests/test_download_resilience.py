import hashlib
import http.server
import os
import platform
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import downloads  # noqa: E402


class _FaultHTTPServer:
    """可控故障注入本地 HTTP 服务器。"""

    def __init__(self, handler_cls):
        self._httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler_cls)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/artifact.bin"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self._httpd.shutdown()
        self._httpd.server_close()


class TestDownloadFaultInjection(unittest.TestCase):
    def test_mid_stream_eof_and_206_resume(self):
        payload = os.urandom(200 * 1024)
        expected_sha = hashlib.sha256(payload).hexdigest()
        state = {"calls": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                state["calls"] += 1
                rng = self.headers.get("Range")
                start = 0
                if rng and rng.startswith("bytes="):
                    start = int(rng.split("=")[1].split("-")[0])
                remaining = payload[start:]
                if state["calls"] == 1:
                    # First attempt: send 64 KB then close early (EOF)
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(remaining[:65536])
                    self.wfile.flush()
                    return
                # Second attempt: honor Range with 206
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(remaining)))
                if start:
                    self.send_header("Content-Range", f"bytes {start}-{len(payload)-1}/{len(payload)}")
                self.end_headers()
                self.wfile.write(remaining)

        with _FaultHTTPServer(Handler) as srv, tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "file.bin"
            res = downloads.fetch_verified(
                [srv.url], dest, sha256=expected_sha, size=len(payload), backoff=0.0,
            )
            self.assertEqual(res, "downloaded")
            self.assertEqual(dest.read_bytes(), payload)
            self.assertEqual(state["calls"], 2)

    def test_no_range_fallback_200(self):
        payload = os.urandom(128 * 1024)
        expected_sha = hashlib.sha256(payload).hexdigest()
        state = {"calls": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                state["calls"] += 1
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if state["calls"] == 1:
                    self.wfile.write(payload[:32768])
                else:
                    # Ignores Range and returns full 200
                    self.wfile.write(payload)

        with _FaultHTTPServer(Handler) as srv, tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "file.bin"
            res = downloads.fetch_verified(
                [srv.url], dest, sha256=expected_sha, size=len(payload), backoff=0.0,
            )
            self.assertEqual(res, "downloaded")
            self.assertEqual(dest.read_bytes(), payload)
            self.assertEqual(state["calls"], 2)

    def test_progress_resets_no_progress_counter_beyond_5_drops(self):
        # 8 drops, each sending 16 KB -> exceeds consecutive limit (5) but succeeds because each makes progress
        chunk_sz = 16 * 1024
        payload = os.urandom(chunk_sz * 8)
        expected_sha = hashlib.sha256(payload).hexdigest()
        state = {"calls": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                state["calls"] += 1
                rng = self.headers.get("Range")
                start = int(rng.split("=")[1].split("-")[0]) if (rng and rng.startswith("bytes=")) else 0
                remaining = payload[start:]
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(remaining)))
                self.end_headers()
                self.wfile.write(remaining[:chunk_sz])

        with _FaultHTTPServer(Handler) as srv, tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "file.bin"
            res = downloads.fetch_verified(
                [srv.url], dest, sha256=expected_sha, size=len(payload), retries=5, max_total_retries=30, backoff=0.0,
            )
            self.assertEqual(res, "downloaded")
            self.assertEqual(dest.read_bytes(), payload)
            self.assertEqual(state["calls"], 8)

    def test_stall_breaker_disconnects_and_resumes(self):
        payload = b"A" * 8192
        expected_sha = hashlib.sha256(payload).hexdigest()
        calls = {"n": 0}

        class FakeResp:
            def __init__(self, chunks, status=200, total=8192):
                self._chunks = list(chunks)
                self.status = status
                self.headers = {"Content-Length": str(total)}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, _n):
                return self._chunks.pop(0) if self._chunks else b""

        now = [0.0]

        def fake_clock():
            return now[0]

        def fake_opener(url, start, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                # 1 KB over 35s -> ~29 B/s < 16 KB/s -> triggers stall breaker
                class SlowResp(FakeResp):
                    def read(self, _n):
                        if self._chunks:
                            now[0] += 35.0
                            return self._chunks.pop(0)
                        return b""

                return SlowResp([payload[:1024], payload[1024:2048]], status=200, total=len(payload))
            # 2nd attempt resumes from 1024 and finishes immediately
            return FakeResp([payload[start:]], status=206, total=len(payload) - start)

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "file.bin"
            logs = []
            res = downloads.fetch_verified(
                ["https://example.invalid/file.bin"],
                dest,
                sha256=expected_sha,
                size=len(payload),
                opener=fake_opener,
                backoff=0.0,
                log=logs.append,
                stall_speed_bytes=16 * 1024,
                stall_seconds=30.0,
                clock=fake_clock,
            )
            self.assertEqual(res, "downloaded")
            self.assertEqual(dest.read_bytes(), payload)
            self.assertEqual(calls["n"], 2)
            self.assertTrue(any("速率过低" in m for m in logs))

    def test_sha_tampering_rejected_and_part_deleted(self):
        payload = b"legitimate-bytes" * 1024
        tampered = b"tampered-bytes!!" * 1024
        expected_sha = hashlib.sha256(payload).hexdigest()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(tampered)))
                self.end_headers()
                self.wfile.write(tampered)

        with _FaultHTTPServer(Handler) as srv, tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "file.bin"
            with self.assertRaises(downloads.DownloadError):
                downloads.fetch_verified([srv.url], dest, sha256=expected_sha, size=len(tampered), backoff=0.0)
            self.assertFalse(dest.exists())
            self.assertFalse(Path(str(dest) + ".part").exists())

    def test_fetch_json_retries_3_times(self):
        state = {"calls": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                state["calls"] += 1
                if state["calls"] < 3:
                    self.send_response(503)
                    self.end_headers()
                    return
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with _FaultHTTPServer(Handler) as srv:
            data = downloads.fetch_json(srv.url, retries=3, backoff=0.0)
            self.assertEqual(data, {"ok": True})
            self.assertEqual(state["calls"], 3)

    def test_slow_download_tip_includes_stage(self):
        tip_img = downloads.slow_download_tip("镜像 cchouse-debian13-arm64.tvm", 0.12)
        self.assertIn("镜像", tip_img)
        tip_cc = downloads.slow_download_tip("Claude Code linux-arm64", 0.08)
        self.assertIn("Claude Code", tip_cc)


@unittest.skipIf(platform.system() != "Darwin", "macOS bootstrap script curl fallback test")
class TestMacBootstrapCurlFallback(unittest.TestCase):
    def test_curl_exit_92_switches_to_http11_and_resumes(self):
        script_text = (ROOT / "scripts" / "bootstrap-macos.sh").read_text(encoding="utf-8")
        # Extract the fetch() function and run it in a subshell with a stubbed curl
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            payload = b"hello-bootstrap-resume-test" * 100
            sha = hashlib.sha256(payload).hexdigest()
            calls_log = td / "curl_calls.txt"
            payload_file = td / "payload.bin"
            payload_file.write_bytes(payload)

            stub_curl = td / "fake_curl.sh"
            stub_curl.write_text(
                f"""#!/bin/bash
echo "$*" >> "{calls_log}"
out=""
has_http11=0
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; shift 2; continue; fi
  if [ "$1" = "--http1.1" ]; then has_http11=1; fi
  shift
done
count=$(wc -l < "{calls_log}" | tr -d ' ')
if [ "$count" -eq 1 ]; then
  head -c 50 "{payload_file}" > "$out"
  exit 92
fi
if [ "$has_http11" -eq 1 ]; then
  tail -c +51 "{payload_file}" >> "$out"
  exit 0
fi
exit 56
""",
                encoding="utf-8",
            )
            os.chmod(stub_curl, 0o755)

            # Build a tiny harness that sources only verified() and fetch() from bootstrap-macos.sh
            start_idx = script_text.index("verified() {")
            end_idx = script_text.index("install_tar() {")
            fn_block = script_text[start_idx:end_idx]

            harness = td / "harness.sh"
            dest = td / "dest.bin"
            harness.write_text(
                f"""#!/bin/bash
set -euo pipefail
bootstrap_mirror=""
curl_bin="{stub_curl}"
fetch_max_attempts=6
fetch_base_backoff=0
{fn_block}
fetch "测试依赖" "https://example.invalid/test.bin" "{sha}" "{dest}"
""",
                encoding="utf-8",
            )
            proc = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(dest.read_bytes(), payload)
            calls = calls_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(calls), 2)
            self.assertNotIn("--http1.1", calls[0])
            self.assertIn("--http1.1", calls[1])
            self.assertIn("-C -", calls[0])
            self.assertIn("--speed-limit 10240", calls[0])
            self.assertIn("--speed-time 30", calls[0])


if __name__ == "__main__":
    unittest.main()
