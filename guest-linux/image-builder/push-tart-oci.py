#!/usr/bin/env python3
"""Package a raw arm64 Linux disk image as a Tart-compatible OCI artifact and push to GHCR.

Tart's OCI format (disk.v2):
- Manifest mediaType: application/vnd.oci.image.manifest.v1+json
- OCI config mediaType: application/vnd.oci.image.config.v1+json
- Layers:
  1. application/vnd.cirruslabs.tart.config.v1 (VM config.json)
  2..N. application/vnd.cirruslabs.tart.disk.v2 (512 MiB chunks compressed in Apple bv41/bv4- LZ4 stream)
  N+1. application/vnd.cirruslabs.tart.nvram.v1 (128 KiB empty Apple VZEFIVariableStore NVRAM)
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib

CHUNK_SIZE = 512 * 1024 * 1024  # 512 MiB (matches Tart DiskV2.layerLimitBytes)
LZ4_BLOCK_SIZE = 65536          # 64 KiB (matches Apple COMPRESSION_LZ4 bv41 blocks)

# Deterministic 128 KiB empty Apple VZEFIVariableStore (EDK2 UEFI FV + FTW headers, zero user state).
EMPTY_LINUX_NVRAM_ZLIB_B64 = (
    "eNrt2D1oU1EYBuDTH3CRxkEcdNKCVRKcHAsOEU1obV0sohXFpuBioqmN/+DiVASRVoyDraNdXBQ6ieDuqgUhHRREiiAudvBez20j"
    "Ygc7Cvo8cO499zvffTnzF8Lv7uS/pPcbU4NPbvcd3nRy+GgInav104dGSmnSHUohvxI/O3uy4pZf/10pfJ2/Pv2w+PjWvq1Dry7f"
    "W2iFcCJZO5s/diB0hT/bFldHXJuXGy/3XGgW5+oXP48enJothskwES6FWjgfjsRnJYxn7WmW2bdBZnbH7riqH14svptcLD9783z/"
    "ztzYtbGYUY+ZlXC23ZGu5vVukLe9fcel9xN3l89UB5pdg7v6d+f6R2JeNabVYupA3F+NNx4Kjaw7BQAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPhfBAAAAOCfl997/NG5GzfLc6/Hw/fZHTOFp4VKkqbpUm7t/Fvc"
    "Nz6NJvXhj+UHtcLbZis5lc0NOtblrKybJ5Ta74XWz4pZCwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMDf9APWFc6z"
)


def empty_linux_nvram_bytes() -> bytes:
    data = zlib.decompress(base64.b64decode(EMPTY_LINUX_NVRAM_ZLIB_B64))
    if len(data) != 131072:
        raise RuntimeError(f"Unexpected NVRAM size: {len(data)}")
    return data


def compress_apple_lz4_bv4(data: bytes) -> bytes:
    """Compress raw bytes into Apple Compression framework LZ4 (bv41/bv4-/bv4$) format."""
    import lz4.block

    parts = []
    mv = memoryview(data)
    total = len(mv)
    for off in range(0, total, LZ4_BLOCK_SIZE):
        blk = mv[off:off + LZ4_BLOCK_SIZE]
        blk_len = len(blk)
        comp = lz4.block.compress(blk, store_size=False)
        if len(comp) < blk_len:
            parts.append(b"bv41")
            parts.append(struct.pack("<II", blk_len, len(comp)))
            parts.append(comp)
        else:
            parts.append(b"bv4-")
            parts.append(struct.pack("<I", blk_len))
            parts.append(bytes(blk))
    parts.append(b"bv4$")
    return b"".join(parts)


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def parse_image_ref(ref: str):
    if "/" not in ref:
        raise ValueError(f"Invalid image reference: {ref}")
    registry, rest = ref.split("/", 1)
    if ":" not in rest:
        raise ValueError(f"Image reference must include a tag: {ref}")
    repo, tag = rest.rsplit(":", 1)
    if repo != repo.lower():
        raise ValueError(f"OCI repository path must be lowercase: {repo}")
    return registry, repo, tag


class OCIClient:
    def __init__(self, registry: str, repo: str, username: str, token: str):
        self.registry = registry
        self.repo = repo
        self.username = username
        self.token = token
        self.bearer = self._get_bearer_token()

    def _get_bearer_token(self) -> str:
        url = f"https://{self.registry}/token?scope=repository:{self.repo}:pull,push"
        auth = base64.b64encode(f"{self.username}:{self.token}".encode("utf-8")).decode("ascii")
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        tok = payload.get("token") or payload.get("access_token")
        if not tok:
            raise RuntimeError("Failed to obtain OCI registry bearer token")
        return tok

    def _headers(self, extra=None):
        h = {"Authorization": f"Bearer {self.bearer}"}
        if extra:
            h.update(extra)
        return h

    def blob_exists(self, digest: str) -> bool:
        url = f"https://{self.registry}/v2/{self.repo}/blobs/{digest}"
        req = urllib.request.Request(url, method="HEAD", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise

    def push_blob_file(self, path: str, digest: str, size: int) -> bool:
        if self.blob_exists(digest):
            return False
        init_url = f"https://{self.registry}/v2/{self.repo}/blobs/uploads/"
        req = urllib.request.Request(init_url, method="POST", data=b"", headers=self._headers())
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 202):
                raise RuntimeError(f"Blob upload init failed: HTTP {resp.status}")
            loc = resp.headers.get("Location")
        if not loc:
            raise RuntimeError("Registry did not return upload Location header")
        upload_url = urllib.parse.urljoin(f"https://{self.registry}", loc)
        sep = "&" if "?" in upload_url else "?"
        final_url = f"{upload_url}{sep}digest={urllib.parse.quote(digest, safe=':')}"

        cmd = [
            "curl", "-fsSL", "--retry", "3", "--retry-delay", "5",
            "-X", "PUT",
            "-H", f"Authorization: Bearer {self.bearer}",
            "-H", "Content-Type: application/octet-stream",
            "-H", f"Content-Length: {size}",
            "-T", path,
            final_url,
        ]
        cp = subprocess.run(cmd, capture_output=True, text=True)
        if cp.returncode != 0:
            raise RuntimeError(f"curl blob upload failed ({digest}): {cp.stderr.strip()}")
        return True

    def push_blob_bytes(self, data: bytes, digest: str) -> bool:
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(data)
            tmp_path = tf.name
        try:
            return self.push_blob_file(tmp_path, digest, len(data))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def push_manifest(self, tag: str, manifest_bytes: bytes) -> str:
        url = f"https://{self.registry}/v2/{self.repo}/manifests/{tag}"
        req = urllib.request.Request(
            url,
            method="PUT",
            data=manifest_bytes,
            headers=self._headers({"Content-Type": "application/vnd.oci.image.manifest.v1+json"}),
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(f"Manifest push failed: HTTP {resp.status}")
            return resp.headers.get("Docker-Content-Digest") or sha256_digest(manifest_bytes)


def build_and_push(args) -> dict:
    registry, repo, tag = parse_image_ref(args.image)
    disk_size = os.path.getsize(args.disk)
    if disk_size <= 0:
        raise RuntimeError(f"Disk image is empty: {args.disk}")

    w_str, h_str = args.display.lower().split("x", 1)
    width, height = int(w_str), int(h_str)

    oci_config_bytes = json.dumps(
        {
            "architecture": "arm64",
            "config": {"Labels": {"org.cirruslabs.tart.disk.format": "raw"}},
            "os": "linux",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    oci_config_digest = sha256_digest(oci_config_bytes)

    tart_config_bytes = json.dumps(
        {
            "arch": "arm64",
            "cpuCount": args.cpu,
            "cpuCountMin": args.cpu,
            "diskFormat": "raw",
            "display": {"height": height, "width": width},
            "macAddress": "02:cc:00:00:00:01",
            "memorySize": args.memory_mb * 1024 * 1024,
            "memorySizeMin": args.memory_mb * 1024 * 1024,
            "os": "linux",
            "version": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    tart_config_digest = sha256_digest(tart_config_bytes)

    nvram_bytes = empty_linux_nvram_bytes()
    nvram_digest = sha256_digest(nvram_bytes)

    client = None
    if not args.dry_run:
        username = os.environ.get("GHCR_USERNAME") or repo.split("/", 1)[0]
        token = os.environ.get("GHCR_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        if not token:
            raise RuntimeError("Missing GHCR_TOKEN / GITHUB_TOKEN for registry push")
        client = OCIClient(registry, repo, username, token)
        client.push_blob_bytes(oci_config_bytes, oci_config_digest)
        client.push_blob_bytes(tart_config_bytes, tart_config_digest)
        client.push_blob_bytes(nvram_bytes, nvram_digest)

    layers = [
        {
            "annotations": {},
            "digest": tart_config_digest,
            "mediaType": "application/vnd.cirruslabs.tart.config.v1",
            "size": len(tart_config_bytes),
        }
    ]

    # Cache identical uncompressed chunks (e.g. all-zero 512 MiB chunks) to skip re-compression & re-upload.
    chunk_cache = {}
    unique_uploaded_bytes = len(oci_config_bytes) + len(tart_config_bytes) + len(nvram_bytes)
    manifest_compressed_bytes = len(tart_config_bytes) + len(nvram_bytes)
    num_chunks = (disk_size + CHUNK_SIZE - 1) // CHUNK_SIZE

    with open(args.disk, "rb") as f:
        idx = 0
        while True:
            raw_chunk = f.read(CHUNK_SIZE)
            if not raw_chunk:
                break
            idx += 1
            uncomp_len = len(raw_chunk)
            uncomp_digest = sha256_digest(raw_chunk)

            if uncomp_digest in chunk_cache:
                comp_digest, comp_len = chunk_cache[uncomp_digest]
                print(f"  chunk {idx}/{num_chunks}: dedup hit ({comp_len} bytes compressed)", flush=True)
            else:
                comp_bytes = compress_apple_lz4_bv4(raw_chunk)
                comp_len = len(comp_bytes)
                comp_digest = sha256_digest(comp_bytes)
                chunk_cache[uncomp_digest] = (comp_digest, comp_len)
                unique_uploaded_bytes += comp_len
                if client is not None:
                    uploaded = client.push_blob_bytes(comp_bytes, comp_digest)
                    status = "uploaded" if uploaded else "already in registry"
                else:
                    status = "dry-run"
                print(
                    f"  chunk {idx}/{num_chunks}: {uncomp_len} -> {comp_len} bytes ({status})",
                    flush=True,
                )
                del comp_bytes

            manifest_compressed_bytes += comp_len
            layers.append(
                {
                    "annotations": {
                        "org.cirruslabs.tart.uncompressed-content-digest": uncomp_digest,
                        "org.cirruslabs.tart.uncompressed-size": str(uncomp_len),
                    },
                    "digest": comp_digest,
                    "mediaType": "application/vnd.cirruslabs.tart.disk.v2",
                    "size": comp_len,
                }
            )

    layers.append(
        {
            "annotations": {},
            "digest": nvram_digest,
            "mediaType": "application/vnd.cirruslabs.tart.nvram.v1",
            "size": len(nvram_bytes),
        }
    )

    upload_time = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "annotations": {
            "org.cirruslabs.tart.uncompressed-disk-size": str(disk_size),
            "org.cirruslabs.tart.upload-time": upload_time,
        },
        "config": {
            "digest": oci_config_digest,
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "size": len(oci_config_bytes),
        },
        "layers": layers,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "schemaVersion": 2,
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8")

    if client is not None:
        manifest_digest = client.push_manifest(tag, manifest_bytes)
    else:
        manifest_digest = sha256_digest(manifest_bytes)

    result = {
        "image": args.image,
        "manifest_digest": manifest_digest,
        "uncompressed_disk_bytes": disk_size,
        "manifest_compressed_bytes": manifest_compressed_bytes,
        "unique_blob_bytes": unique_uploaded_bytes,
        "unique_disk_chunks": len(chunk_cache),
        "total_disk_chunks": num_chunks,
    }
    if args.manifest_out:
        with open(args.manifest_out, "wb") as mf:
            mf.write(manifest_bytes)
    return result


def main():
    p = argparse.ArgumentParser(description="Push raw arm64 Linux disk as Tart OCI image to GHCR")
    p.add_argument("--disk", required=True, help="Path to raw disk image (disk.raw)")
    p.add_argument("--image", required=True, help="Target OCI ref, e.g. ghcr.io/nana-fox/cchouse-debian13-arm64:ci-test")
    p.add_argument("--cpu", type=int, default=4)
    p.add_argument("--memory-mb", type=int, default=4096)
    p.add_argument("--display", default="1440x900")
    p.add_argument("--dry-run", action="store_true", help="Build manifest locally without pushing to registry")
    p.add_argument("--manifest-out", default="", help="Optional path to write rendered OCI manifest JSON")
    args = p.parse_args()

    res = build_and_push(args)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
