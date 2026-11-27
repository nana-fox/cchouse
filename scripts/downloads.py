#!/usr/bin/env python3
"""宿主机下载与校验公共逻辑（scripts/fetch-claude-code.py 与 scripts/cchouse.py up 共用）。

- 断点续传：先写 `<目标>.part`，再次运行用 HTTP Range 从断点继续；服务器不支持 Range 时自动从头下载。
- 校验后原子落盘：SHA-256（黄金镜像）或 npm SRI sha512（Claude Code 包）校验通过才 rename 成正式文件，失败删除 `.part`。
- 已下载且校验通过就跳过：校验结果记入 `<目标>.verified`（摘要 + 大小 + mtime），文件未变时不重复整包哈希。
- 走宿主机网络，默认跟随用户环境（系统 HTTP(S)_PROXY），不强制直连或代理，不经过用户的出口节点；不写日志、不打印凭据。
- 实时速度：Progress.render() 带最近 5 秒速度；开始 10 秒内平均低于 1 MB/s 提示一次可换直连或镜像源。
- 镜像源：CCHOUSE_IMAGE_MIRROR（黄金镜像目录 URL）、CCHOUSE_NPM_MIRROR（npm registry 镜像），校验值仍以官方为准。
"""
import base64
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

USER_AGENT = "cchouse-fetch/1"
CHUNK = 1024 * 1024
NPM_REGISTRY = "https://registry.npmjs.org/"
IMAGE_MIRROR_ENV = "CCHOUSE_IMAGE_MIRROR"
NPM_MIRROR_ENV = "CCHOUSE_NPM_MIRROR"
SLOW_CHECK_SECONDS = 10
SLOW_THRESHOLD_MB_S = 1.0
STALL_SPEED_BYTES = 16 * 1024  # 16 KB/s
STALL_SECONDS = 30.0
MAX_TOTAL_RETRIES = 30
MAX_NO_PROGRESS_RETRIES = 5
MAX_BACKOFF_SECONDS = 30.0

CLAUDE_PLATFORMS = {
    "linux-x64": "@anthropic-ai/claude-code-linux-x64",
    "linux-arm64": "@anthropic-ai/claude-code-linux-arm64",
    "darwin-arm64": "@anthropic-ai/claude-code-darwin-arm64",
    "darwin-x64": "@anthropic-ai/claude-code-darwin-x64",
}


class DownloadError(RuntimeError):
    pass


class Progress:
    """线程安全的下载进度（后台线程写，主线程读并打印）。"""

    def __init__(self, label):
        self.label = label
        self.done = 0
        self.start = 0  # 本次续传起点（字节），用于计算本次实际下载量与速度
        self.total = 0
        self.state = "pending"  # pending | downloading | verifying | ok | skipped | failed
        self.error = ""
        self.result = None
        self.started = None  # 本次开始传输的 monotonic 时间
        self._samples = []  # (t, done) 最近几秒，用于实时速度
        self._slow_hint_given = False
        self._lock = threading.Lock()

    def update(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)
            if "done" in kw and self.state == "downloading":
                now = time.monotonic()
                if self.started is None:
                    self.started = now
                self._samples.append((now, self.done))
                while len(self._samples) > 2 and now - self._samples[0][0] > 5:
                    self._samples.pop(0)

    def speed(self):
        """最近约 5 秒的实时速度（MB/s）。"""
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
            return (d1 - d0) / 1048576 / (t1 - t0) if t1 > t0 else 0.0

    def slow_hint(self, threshold=SLOW_THRESHOLD_MB_S, after=SLOW_CHECK_SECONDS):
        """开始传输满 after 秒后平均速度低于阈值时返回一次 MB/s 数值，否则 None（只提示一次）。"""
        with self._lock:
            if self._slow_hint_given or self.started is None or self.state != "downloading":
                return None
            elapsed = time.monotonic() - self.started
            if elapsed < after:
                return None
            self._slow_hint_given = True
            avg = (self.done - self.start) / 1048576 / elapsed
            return avg if avg < threshold else None

    def snapshot(self):
        with self._lock:
            return {"label": self.label, "done": self.done, "total": self.total, "state": self.state, "error": self.error}

    def render(self):
        s = self.snapshot()
        if s["state"] in ("ok", "skipped"):
            return f"{s['label']} 已就绪"
        if s["state"] == "failed":
            return f"{s['label']} 失败：{s['error']}"
        if s["state"] == "verifying":
            return f"{s['label']} 校验中"
        mb = s["done"] / 1048576
        spd = f" {self.speed():.2f} MB/s" if s["state"] == "downloading" else ""
        if s["total"]:
            pct = min(100, int(s["done"] * 100 / s["total"]))
            return f"{s['label']} {pct}% ({mb:.0f}/{s['total'] / 1048576:.0f} MB){spd}"
        return f"{s['label']} {mb:.0f} MB{spd}" if s["done"] else f"{s['label']} 等待中"


def _hash_file(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h


def require_sha256(path, expected):
    """核对本地文件的 SHA-256。不匹配就失败，不发起下载。"""
    expected = (expected or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise DownloadError("SHA-256 格式无效")
    digest = sha256_file(path)
    if digest != expected:
        raise DownloadError("SHA-256 校验不通过")
    return digest


def sha256_file(path):
    return _hash_file(path, "sha256").hexdigest()


def sri_sha512_file(path):
    return "sha512-" + base64.b64encode(_hash_file(path, "sha512").digest()).decode("ascii")


def verify_sri(data, integrity):
    """校验内存数据的 npm SRI（sha512-<base64>）。"""
    if not integrity.startswith("sha512-"):
        raise DownloadError(f"不支持的 integrity 算法：{integrity}")
    actual = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if actual != integrity:
        raise DownloadError(f"SHA-512 integrity 校验失败（期望 {integrity[7:23]}...，实际 {actual[7:23]}...）")


def _digest_matches(path, sha256=None, integrity=None):
    if sha256:
        return sha256_file(path).lower() == sha256.lower()
    if integrity:
        if not integrity.startswith("sha512-"):
            raise DownloadError(f"不支持的 integrity 算法：{integrity}")
        return sri_sha512_file(path) == integrity
    raise DownloadError("必须提供 sha256 或 integrity 之一")


def _marker_path(dest):
    return Path(str(dest) + ".verified")


def _write_marker(dest, digest):
    st = Path(dest).stat()
    _marker_path(dest).write_text(
        json.dumps({"digest": digest, "size": st.st_size, "mtime_ns": st.st_mtime_ns}) + "\n", encoding="utf-8"
    )


def is_verified(dest, sha256=None, integrity=None, trust_marker=True):
    """目标文件存在且摘要匹配即返回 True；marker 与文件 size/mtime 一致时免整包哈希。"""
    dest = Path(dest)
    if not dest.is_file():
        return False
    digest = sha256 or integrity
    marker = _marker_path(dest)
    if trust_marker and marker.is_file():
        try:
            m = json.loads(marker.read_text(encoding="utf-8"))
            st = dest.stat()
            if m.get("digest") == digest and m.get("size") == st.st_size and m.get("mtime_ns") == st.st_mtime_ns:
                return True
        except (OSError, ValueError):
            pass
    if _digest_matches(dest, sha256=sha256, integrity=integrity):
        _write_marker(dest, digest)
        return True
    return False


def _open(url, start, timeout):
    headers = {"User-Agent": USER_AGENT}
    if start:
        headers["Range"] = f"bytes={start}-"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def download_resumable(url, part_path, expected_size=None, progress=None, timeout=60, opener=None, log=None,
                       stall_speed_bytes=STALL_SPEED_BYTES, stall_seconds=STALL_SECONDS, clock=None):
    """下载到 part_path，已有部分则用 Range 续传；返回最终字节数。

    - 连接提前结束（< 期望大小）抛 DownloadError 且保留 .part。
    - 低速僵死熔断：连续 stall_seconds 秒速率低于 stall_speed_bytes 主动断开，保留 .part 交给调用方续传。
    """
    opener = opener or _open
    now_fn = clock or time.monotonic
    part_path = Path(part_path)
    start = part_path.stat().st_size if part_path.is_file() else 0
    if expected_size and start > expected_size:
        part_path.unlink()
        start = 0
    if expected_size and start == expected_size:
        return start
    effective_timeout = min(timeout, stall_seconds) if stall_seconds and stall_seconds > 0 else timeout
    try:
        resp = opener(url, start, effective_timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and start:
            # Range 不可满足：多半是 .part 已完整或损坏，交给校验决定；校验失败会删除重下
            return start
        raise DownloadError(f"HTTP {exc.code}：{url}（已下载部分保留，再次运行会断点续传）") from None
    except (urllib.error.URLError, OSError) as exc:
        raise DownloadError(f"网络错误（{type(exc).__name__}）：{url}（已下载部分保留，再次运行会断点续传）") from None
    with resp:
        status = getattr(resp, "status", None) or resp.getcode()
        if start and status != 206:
            if log:
                log(f"服务器不支持断点续传（HTTP {status}），从头重新下载")
            start = 0
        mode = "ab" if start else "wb"
        length = resp.headers.get("Content-Length")
        total = expected_size or (start + int(length) if length and length.isdigit() else 0)
        done = start
        if progress:
            progress.update(state="downloading", done=done, start=start, total=total)
        win_start = now_fn()
        win_bytes = 0
        with open(part_path, mode) as f:
            while True:
                try:
                    chunk = resp.read(CHUNK)
                except OSError as exc:
                    raise DownloadError(f"下载中断（{type(exc).__name__}），再次运行会从断点续传") from None
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                win_bytes += len(chunk)
                if progress:
                    progress.update(done=done)
                if stall_seconds and stall_seconds > 0 and stall_speed_bytes and stall_speed_bytes > 0:
                    now = now_fn()
                    elapsed = now - win_start
                    if elapsed >= stall_seconds:
                        rate = win_bytes / elapsed
                        if rate < stall_speed_bytes:
                            raise DownloadError(
                                f"下载速率过低（持续 {elapsed:.0f}s 仅 {rate / 1024:.1f} KB/s，"
                                f"低于 {stall_speed_bytes // 1024} KB/s 阈值），已主动断开并从断点续传"
                            )
                        win_start = now
                        win_bytes = 0
    if total and done < total:
        # 代理/服务器提前正常关闭连接（EOF 无异常）：保留 .part，交给调用方续传，不能当作下载完成去校验
        raise DownloadError(f"连接提前结束（{done}/{total} 字节），已下载部分保留，可从断点续传")
    return done


def fetch_verified(urls, dest, sha256=None, integrity=None, size=None, progress=None, opener=None,
                   retries=MAX_NO_PROGRESS_RETRIES, backoff=2.0, log=None,
                   max_total_retries=MAX_TOTAL_RETRIES,
                   stall_speed_bytes=STALL_SPEED_BYTES, stall_seconds=STALL_SECONDS, clock=None):
    """按 URL 列表依次尝试，断点续传 + 校验后原子落盘；已校验通过则直接跳过。返回 'skipped' 或 'downloaded'。

    - 进度感知重试：只要本次中断前写入了新字节，就重置连续无进展失败计数（连续无进展上限 retries=5，总上限 max_total_retries=30）。
    - 低速僵死熔断：连续 30s < 16 KB/s 自动断开并以 Range 续传。
    - 只有大小完整但摘要不符才删除 .part。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if progress:
        progress.update(total=size or 0)
    if is_verified(dest, sha256=sha256, integrity=integrity):
        if progress:
            progress.update(state="skipped", done=size or dest.stat().st_size)
        return "skipped"
    part = Path(str(dest) + ".part")
    label = progress.label if progress else dest.name
    last_err = None
    for url in urls:
        completed = False
        total_failures = 0
        no_progress_failures = 0
        while True:
            before = part.stat().st_size if part.is_file() else 0
            try:
                download_resumable(
                    url, part, expected_size=size, progress=progress, opener=opener, log=log,
                    stall_speed_bytes=stall_speed_bytes, stall_seconds=stall_seconds, clock=clock,
                )
                completed = True
                break
            except DownloadError as exc:
                last_err = exc
                if retries <= 0 or max_total_retries <= 0:
                    break
                after = part.stat().st_size if part.is_file() else 0
                total_failures += 1
                if after > before:
                    no_progress_failures = 0
                else:
                    no_progress_failures += 1
                if no_progress_failures > retries or total_failures > max_total_retries:
                    break
                exp = max(0, no_progress_failures - 1)
                wait = min(MAX_BACKOFF_SECONDS, backoff * (2 ** exp))
                if log:
                    prog_note = "有新进展，重置无进展计数" if after > before else f"连续无进展 {no_progress_failures}/{retries}"
                    log(
                        f"{label} 第 {total_failures}/{max_total_retries} 次续传"
                        f"（{prog_note}，{wait:.0f}s 后从 {after / 1048576:.1f} MB 处继续）：{last_err}"
                    )
                if wait > 0:
                    time.sleep(wait)
        if not completed:
            continue
        if progress:
            progress.update(state="verifying")
        if _digest_matches(part, sha256=sha256, integrity=integrity):
            os.replace(part, dest)
            if os.name != "nt":
                os.chmod(dest, 0o600)
            _write_marker(dest, sha256 or integrity)
            if progress:
                progress.update(state="ok")
            return "downloaded"
        part.unlink()
        last_err = DownloadError(f"{label} 下载大小完整但校验不通过，已删除损坏文件；再次运行会重新下载")
    if progress:
        progress.update(state="failed", error=str(last_err))
    raise last_err or DownloadError(f"{label} 没有可用的下载地址")


def fetch_json(url, timeout=30, retries=3, backoff=1.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    last_exc = None
    for attempt in range(max(1, int(retries))):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code < 500 and exc.code != 429:
                raise
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            last_exc = exc
        if attempt + 1 < max(1, int(retries)):
            time.sleep(backoff * (2 ** attempt))
    raise last_exc


def claude_meta(platform_key, version="latest", fetch=fetch_json):
    pkg = CLAUDE_PLATFORMS[platform_key]
    meta_url = f"https://registry.npmjs.org/{pkg.replace('/', '%2f')}/{version}"
    meta = fetch(meta_url)
    dist = meta.get("dist") or {}
    if not dist.get("tarball") or not dist.get("integrity"):
        raise DownloadError(f"npm 元数据缺少 tarball 或 integrity：{meta_url}")
    return {"package": pkg, "version": meta.get("version"), "tarball": dist["tarball"], "integrity": dist["integrity"]}


def network_label():
    """当前下载走哪条网络：跟随用户环境（HTTP(S)_PROXY / 系统代理），不强制直连或代理。"""
    proxies = urllib.request.getproxies()
    return "宿主代理" if (proxies.get("https") or proxies.get("all")) else "直连"


def _stage_label(label):
    if label.startswith(("引导依赖", "镜像", "Claude Code")):
        return label
    low = label.lower()
    if "claude" in low:
        return f"Claude Code（{label}）"
    if "image" in low or ".tvm" in low or ".ova" in low:
        return f"镜像（{label}）"
    return label


def slow_download_tip(label, mb_s):
    stage = _stage_label(label)
    return (
        f"提示：[{stage}] 前 {SLOW_CHECK_SECONDS} 秒平均仅 {mb_s:.2f} MB/s（当前网络：{network_label()}）。"
        f"可 Ctrl-C 后换直连（临时取消 HTTPS_PROXY/ALL_PROXY）或指定镜像源"
        f"（--image-mirror / {IMAGE_MIRROR_ENV}，--npm-mirror / {NPM_MIRROR_ENV}）再运行，已下载部分会续传，校验值不变。"
    )


def image_urls(artifact, mirror=None):
    """镜像源（参数或 CCHOUSE_IMAGE_MIRROR，填目录 URL）优先，官方 URL 兜底；SHA-256 始终按 manifest 校验。"""
    mirror = mirror or os.environ.get(IMAGE_MIRROR_ENV, "")
    urls = list(artifact.get("urls") or [])
    if mirror:
        urls.insert(0, mirror.rstrip("/") + "/" + artifact["filename"])
    return urls


def claude_tarball_urls(tarball_url, mirror=None):
    """npm 镜像源（参数或 CCHOUSE_NPM_MIRROR）优先，官方 registry 兜底；元数据与 integrity 只信官方 registry。"""
    mirror = mirror or os.environ.get(NPM_MIRROR_ENV, "")
    urls = [tarball_url]
    if mirror and tarball_url.startswith(NPM_REGISTRY):
        urls.insert(0, mirror.rstrip("/") + "/" + tarball_url[len(NPM_REGISTRY):])
    return urls


def run_with_progress(progress, fn, stream=None, interval=2.0):
    """在后台线程执行 fn(progress)，前台每 interval 秒打印实时进度/速度；慢速时提示一次。返回 fn 结果。"""
    import sys

    stream = stream or sys.stderr
    box = {}

    def target():
        try:
            box["result"] = fn(progress)
        except BaseException as exc:  # noqa: BLE001 - 交回主线程抛出
            box["error"] = exc

    th = threading.Thread(target=target, daemon=True)
    th.start()
    while th.is_alive():
        th.join(interval)
        if th.is_alive():
            print(f"  [下载] {progress.render()}", file=stream, flush=True)
            slow = progress.slow_hint()
            if slow is not None:
                print("  " + slow_download_tip(progress.label, slow), file=stream, flush=True)
    if "error" in box:
        raise box["error"]
    return box.get("result")


def fetch_claude_code(platform_key, out, version="latest", progress=None, fetch=fetch_json, opener=None, mirror=None, log=None):
    """拉官方 npm 平台原生包并核对 SRI；元数据记入 `<out>.json`，离线时可复用已校验的缓存。"""
    out = Path(out)
    meta_file = Path(str(out) + ".json")
    try:
        meta = claude_meta(platform_key, version, fetch=fetch)
    except Exception as exc:
        # registry 不可达：若已有校验通过的缓存则继续使用
        if meta_file.is_file():
            cached = json.loads(meta_file.read_text(encoding="utf-8"))
            if is_verified(out, integrity=cached.get("integrity")):
                if progress:
                    progress.update(state="skipped")
                return dict(cached, out=str(out), status="skipped")
        if progress:
            progress.update(state="failed", error=f"npm 元数据获取失败（{type(exc).__name__}）")
        raise DownloadError(f"npm 元数据获取失败（{type(exc).__name__}）") from None
    status = fetch_verified(
        claude_tarball_urls(meta["tarball"], mirror), out, integrity=meta["integrity"], progress=progress, opener=opener,
        log=log,
    )
    meta_file.write_text(json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8")
    return dict(meta, out=str(out), status=status, bytes=out.stat().st_size)


def load_image_artifact(manifest_path, route):
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    for art in data.get("artifacts", []):
        if art.get("route") == route:
            return dict(art, image_version=data.get("image_version", ""))
    raise DownloadError(f"image-manifest.json 中没有路线 {route} 的镜像")


# NAT-only base package. The PowerShell installer hardcodes the same arguments
# and does not trust this list to add bridged or host-only features.
VBOX_SILENT_ARGS = [
    "--silent",
    "--ignore-reboot",
    "--msiparams",
    "ADDLOCAL=VBoxApplication",
    "VBOX_INSTALLDESKTOPSHORTCUT=0",
    "VBOX_INSTALLQUICKLAUNCHSHORTCUT=0",
    "VBOX_REGISTERFILEEXTENSIONS=0",
    "VBOX_START=0",
]
_VBOX_FORBIDDEN_MARKERS = ("extpack", "extension_pack", "extension pack")


def _vbox_text_forbidden(value):
    low = value.lower()
    return any(marker in low for marker in _VBOX_FORBIDDEN_MARKERS)


def load_virtualbox_installer(manifest_path):
    """读取 VirtualBox 基础包装清单。只接受 Oracle 官网 URL，拒绝附加包与再分发。"""
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DownloadError("virtualbox-manifest.json 必须是对象")
    if data.get("schema_version") != 1:
        raise DownloadError("virtualbox-manifest.json schema_version 必须为 1")
    if data.get("extension_pack") is not False:
        raise DownloadError("virtualbox-manifest.json 必须声明 extension_pack=false")
    if data.get("redistribute") is not False:
        raise DownloadError("virtualbox-manifest.json 必须声明 redistribute=false（安装包只从官网下载，不随仓库分发）")
    version = str(data.get("version") or "")
    build = str(data.get("build") or "")
    filename = str(data.get("filename") or "")
    url = str(data.get("url") or "")
    sha = str(data.get("sha256") or "").lower()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise DownloadError("virtualbox-manifest.json version 无效")
    if not re.fullmatch(r"\d+", build):
        raise DownloadError("virtualbox-manifest.json build 无效")
    if filename != f"VirtualBox-{version}-{build}-Win.exe":
        raise DownloadError("virtualbox-manifest.json filename 与 version/build 不一致")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise DownloadError("virtualbox-manifest.json sha256 无效")
    size = data.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise DownloadError("virtualbox-manifest.json size_bytes 无效")
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host != "download.virtualbox.org":
        raise DownloadError("virtualbox-manifest.json 只允许 https://download.virtualbox.org 官方地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DownloadError("virtualbox-manifest.json URL 含多余成分")
    if parsed.path != f"/virtualbox/{version}/{filename}":
        raise DownloadError("virtualbox-manifest.json URL 与版本或文件名不一致")
    for key, val in data.items():
        if key == "extension_pack":
            continue
        texts = []
        if isinstance(val, str):
            texts.append(val)
        elif isinstance(val, list):
            texts.extend(item for item in val if isinstance(item, str))
        if any(_vbox_text_forbidden(item) for item in texts):
            raise DownloadError("virtualbox-manifest.json 不得包含附加包")
    silent = data.get("silent_args")
    if silent is not None and list(silent) != VBOX_SILENT_ARGS:
        raise DownloadError("virtualbox-manifest.json silent_args 必须是 NAT-only 基础包装参数")
    guest_additions = _guest_additions_from(data, version)
    return {
        "version": version,
        "build": build,
        "filename": filename,
        "sha256": sha,
        "size_bytes": size,
        "url": url,
        "extension_pack": False,
        "redistribute": False,
        "silent_args": list(VBOX_SILENT_ARGS),
        "guest_additions": guest_additions,
    }


def _guest_additions_from(data, version):
    """Guest Additions ISO。只接受同版本官方 ISO，拒绝附加包。"""
    ga = data.get("guest_additions")
    if not isinstance(ga, dict):
        raise DownloadError("virtualbox-manifest.json guest_additions 必须是对象")
    filename = str(ga.get("filename") or "")
    url = str(ga.get("url") or "")
    sha = str(ga.get("sha256") or "").lower()
    expected_name = f"VBoxGuestAdditions_{version}.iso"
    if filename != expected_name:
        raise DownloadError("virtualbox-manifest.json guest_additions filename 与 version 不一致")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise DownloadError("virtualbox-manifest.json guest_additions sha256 无效")
    size = ga.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise DownloadError("virtualbox-manifest.json guest_additions size_bytes 无效")
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host != "download.virtualbox.org":
        raise DownloadError("virtualbox-manifest.json guest_additions 只允许 https://download.virtualbox.org")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DownloadError("virtualbox-manifest.json guest_additions URL 含多余成分")
    if parsed.path != f"/virtualbox/{version}/{filename}":
        raise DownloadError("virtualbox-manifest.json guest_additions URL 与版本或文件名不一致")
    blob = " ".join((filename, url))
    if _vbox_text_forbidden(blob) or url.lower().endswith(".vbox-extpack"):
        raise DownloadError("virtualbox-manifest.json guest_additions 不得是附加包")
    return {
        "filename": filename,
        "sha256": sha,
        "size_bytes": size,
        "url": url,
    }


def host_mihomo_platform():
    sys_name = platform.system()
    machine = platform.machine().lower()
    if sys_name == "Darwin" and machine in ("arm64", "aarch64"):
        return "darwin-arm64"
    if sys_name == "Windows" and machine in ("amd64", "x86_64"):
        return "windows-amd64"
    return None


def load_mihomo_artifact(manifest_path, platform_key=None):
    """读取与客机同版本的宿主临时 mihomo 清单，严格校验官网 URL 与 SHA-256。"""
    key = platform_key or host_mihomo_platform()
    if not key:
        raise DownloadError("当前宿主机平台没有可用的官方 mihomo 探测程序")
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise DownloadError("mihomo-manifest.json schema_version 必须为 1")
    version = str(data.get("version") or "")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise DownloadError("mihomo-manifest.json version 无效")
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, dict) or key not in artifacts:
        raise DownloadError(f"mihomo-manifest.json 中没有平台 {key} 的制品")
    art = artifacts[key]
    if not isinstance(art, dict):
        raise DownloadError("mihomo-manifest.json 制品格式无效")
    filename = str(art.get("filename") or "")
    binary_name = str(art.get("binary_name") or "")
    fmt = str(art.get("format") or "")
    url = str(art.get("url") or "")
    sha = str(art.get("sha256") or "").lower()
    size = art.get("size_bytes")
    if fmt not in ("gz", "zip") or not filename or not binary_name:
        raise DownloadError("mihomo-manifest.json 制品字段缺失")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise DownloadError("mihomo-manifest.json sha256 无效")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise DownloadError("mihomo-manifest.json size_bytes 无效")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "github.com":
        raise DownloadError("mihomo-manifest.json 只允许 https://github.com/MetaCubeX/mihomo 官方地址")
    if parsed.path != f"/MetaCubeX/mihomo/releases/download/{version}/{filename}":
        raise DownloadError("mihomo-manifest.json URL 与版本或文件名不一致")
    return {
        "version": version,
        "platform": key,
        "filename": filename,
        "binary_name": binary_name,
        "format": fmt,
        "size_bytes": size,
        "url": url,
        "sha256": sha,
    }


def ensure_host_mihomo(manifest_path, bootstrap_root, platform_key=None, opener=None, log=None):
    """确保私有 bootstrap/tools 目录下存在经 SHA-256 校验的同版本官方 mihomo，缓存复用。"""
    override = os.environ.get("CCHOUSE_MIHOMO_BIN", "").strip()
    if override and os.path.isfile(override):
        return Path(override)
    art = load_mihomo_artifact(manifest_path, platform_key=platform_key)
    bootstrap_root = Path(bootstrap_root)
    tool_dir = bootstrap_root / "tools" / f"mihomo-{art['version']}"
    bin_path = tool_dir / art["binary_name"]
    if bin_path.is_file():
        return bin_path
    downloads_dir = bootstrap_root / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    archive_path = downloads_dir / f"mihomo-{art['version']}-{art['platform']}-{art['sha256'][:12]}.{art['format']}"
    urls = [art["url"]]
    mirror = os.environ.get("CCHOUSE_BOOTSTRAP_MIRROR", "").strip()
    if mirror:
        urls.insert(0, mirror.rstrip("/") + "/" + art["filename"])
    fetch_verified(
        urls,
        archive_path,
        sha256=art["sha256"],
        size=art["size_bytes"],
        opener=opener,
        log=log,
    )
    tool_dir.mkdir(parents=True, exist_ok=True)
    tmp_bin = tool_dir / f".{art['binary_name']}.tmp.{os.getpid()}"
    try:
        if art["format"] == "gz":
            with gzip.open(archive_path, "rb") as src, open(tmp_bin, "wb") as dst:
                shutil.copyfileobj(src, dst)
        else:
            with zipfile.ZipFile(archive_path, "r") as zf:
                exe_members = [m for m in zf.namelist() if m.lower().endswith(".exe") and not m.endswith("/")]
                if len(exe_members) != 1:
                    raise DownloadError("mihomo Windows 压缩包中未找到唯一的 .exe 文件")
                with zf.open(exe_members[0], "r") as src, open(tmp_bin, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        if os.name != "nt":
            os.chmod(tmp_bin, 0o700)
        os.replace(tmp_bin, bin_path)
    finally:
        if tmp_bin.exists():
            try:
                tmp_bin.unlink()
            except OSError:
                pass
    return bin_path
