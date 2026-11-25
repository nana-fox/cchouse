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
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = "cchouse-fetch/1"
CHUNK = 1024 * 1024
NPM_REGISTRY = "https://registry.npmjs.org/"
IMAGE_MIRROR_ENV = "CCHOUSE_IMAGE_MIRROR"
NPM_MIRROR_ENV = "CCHOUSE_NPM_MIRROR"
SLOW_CHECK_SECONDS = 10
SLOW_THRESHOLD_MB_S = 1.0

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


def download_resumable(url, part_path, expected_size=None, progress=None, timeout=60, opener=None, log=None):
    """下载到 part_path，已有部分则用 Range 续传；返回最终字节数。连接提前结束（< 期望大小）抛 DownloadError 且保留 .part。"""
    opener = opener or _open
    part_path = Path(part_path)
    start = part_path.stat().st_size if part_path.is_file() else 0
    if expected_size and start > expected_size:
        part_path.unlink()
        start = 0
    if expected_size and start == expected_size:
        return start
    try:
        resp = opener(url, start, timeout)
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
                if progress:
                    progress.update(done=done)
    if total and done < total:
        # 代理/服务器提前正常关闭连接（EOF 无异常）：保留 .part，交给调用方续传，不能当作下载完成去校验
        raise DownloadError(f"连接提前结束（{done}/{total} 字节），已下载部分保留，可从断点续传")
    return done


def fetch_verified(urls, dest, sha256=None, integrity=None, size=None, progress=None, opener=None,
                   retries=5, backoff=2.0, log=None):
    """按 URL 列表依次尝试，断点续传 + 校验后原子落盘；已校验通过则直接跳过。返回 'skipped' 或 'downloaded'。

    - 断线或连接提前结束（已下载 < size）：视为中断，不校验、不删 .part，同轮用 Range 自动续传，
      最多 retries 次、指数退避（backoff, 2*backoff, ...），每次打印第几次续传；仍失败再换下一个 URL。
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
        for attempt in range(retries + 1):
            if attempt:
                wait = backoff * (2 ** (attempt - 1))
                have = part.stat().st_size if part.is_file() else 0
                if log:
                    log(f"{label} 第 {attempt}/{retries} 次续传（{wait:.0f}s 后，从 {have / 1048576:.0f} MB 处继续）：{last_err}")
                time.sleep(wait)
            try:
                download_resumable(url, part, expected_size=size, progress=progress, opener=opener, log=log)
                completed = True
                break
            except DownloadError as exc:
                last_err = exc
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
        last_err = DownloadError("下载大小完整但校验不通过，已删除损坏文件；再次运行会重新下载")
    if progress:
        progress.update(state="failed", error=str(last_err))
    raise last_err or DownloadError("没有可用的下载地址")


def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def slow_download_tip(label, mb_s):
    return (
        f"提示：{label} 前 {SLOW_CHECK_SECONDS} 秒平均仅 {mb_s:.2f} MB/s（当前网络：{network_label()}）。"
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
