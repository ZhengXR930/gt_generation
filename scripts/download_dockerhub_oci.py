#!/usr/bin/env python3
"""Download a Docker Hub image as an OCI archive using curl -4 and retry.

This is a workaround for Docker Desktop/daemon EOF failures while pulling from
Docker Hub. It preserves the original manifest/config/layers so `docker load`
can import the tag normally.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REGISTRY = "https://registry-1.docker.io"
AUTH = "https://auth.docker.io/token"
ACCEPT_MANIFEST = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])
RESOLVE_HOSTS = [
    "auth.docker.io",
    "registry-1.docker.io",
    "production.cloudflare.docker.com",
]
_RESOLVE_ARGS: list[str] | None = None


def run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=capture, check=False)


def parse_ref(ref: str) -> tuple[str, str]:
    if "/" not in ref.split(":", 1)[0]:
        ref = "library/" + ref
    if ":" in ref.rsplit("/", 1)[-1]:
        repo, tag = ref.rsplit(":", 1)
    else:
        repo, tag = ref, "latest"
    return repo, tag


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def curl_json(url: str, headers: list[str] | None = None) -> dict:
    cmd = ["curl", "-4", *dockerhub_resolve_args(), "-fsSL", "--retry", "8", "--retry-all-errors", "--retry-delay", "3", url]
    for h in headers or []:
        cmd[1:1] = ["-H", h]
    p = subprocess.run(cmd, text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError(f"curl failed for {url}: {p.stderr.strip()}")
    return json.loads(p.stdout)


def get_auth_header(repo: str) -> str:
    token_url = f"{AUTH}?service=registry.docker.io&scope=repository:{repo}:pull"
    print(f"getting token for {repo}", flush=True)
    token = curl_json(token_url)["token"]
    return f"Authorization: Bearer {token}"


def curl_file(
    url: str,
    dst: Path,
    headers: list[str] | None = None,
    retries: int = 12,
    auth_repo: str | None = None,
) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    for attempt in range(1, retries + 1):
        effective_headers = list(headers or [])
        if auth_repo:
            effective_headers.append(get_auth_header(auth_repo))
        cmd = [
            "curl", "-4", *dockerhub_resolve_args(), "-fL", "--retry", "5", "--retry-all-errors", "--retry-delay", "3",
            "--connect-timeout", "20", "--max-time", "0", "-C", "-", "-o", str(tmp), url,
        ]
        for h in effective_headers:
            cmd[1:1] = ["-H", h]
        print(f"download attempt {attempt}/{retries}: {url}", flush=True)
        p = subprocess.run(cmd, text=True)
        if p.returncode == 0:
            tmp.replace(dst)
            return
        time.sleep(min(30, attempt * 3))
    raise RuntimeError(f"failed to download after {retries} attempts: {url}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", help="Docker Hub image ref, e.g. n132/arvo:13730-vul")
    ap.add_argument("--out", type=Path, default=None, help="Output OCI archive tar")
    ap.add_argument("--work-dir", type=Path, default=Path("tmp/dockerhub_downloads"))
    ap.add_argument("--cache-dir", type=Path, default=Path("tmp/dockerhub_downloads/blob_cache"))
    ap.add_argument("--keep-work", action="store_true", help="Keep per-image OCI work directory after writing archive")
    args = ap.parse_args()

    repo, tag = parse_ref(args.image)
    out = args.out or Path("tmp/dockerhub_downloads") / (args.image.replace("/", "_").replace(":", "_") + ".oci.tar")
    work = args.work_dir / (args.image.replace("/", "_").replace(":", "_"))
    if work.exists():
        shutil.rmtree(work)
    blobs = work / "blobs" / "sha256"
    blobs.mkdir(parents=True, exist_ok=True)
    cache = args.cache_dir / "sha256"
    cache.mkdir(parents=True, exist_ok=True)

    auth_header = get_auth_header(repo)

    manifest_url = f"{REGISTRY}/v2/{repo}/manifests/{tag}"
    manifest_path = work / "manifest.raw.json"
    curl_file(manifest_url, manifest_path, [auth_header, f"Accept: {ACCEPT_MANIFEST}"], retries=8)
    manifest_digest = sha256_file(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    media_type = manifest.get("mediaType", "application/vnd.docker.distribution.manifest.v2+json")
    _link_or_copy(manifest_path, blobs / manifest_digest)
    print(f"manifest sha256:{manifest_digest} mediaType={media_type}", flush=True)

    config_desc = manifest["config"]
    layer_descs = manifest.get("layers", [])
    descriptors = [config_desc] + layer_descs
    for i, desc in enumerate(descriptors):
        digest = desc["digest"]
        algo, hex_digest = digest.split(":", 1)
        if algo != "sha256":
            raise RuntimeError(f"unsupported digest {digest}")
        dst = blobs / hex_digest
        cached = cache / hex_digest
        if cached.exists() and sha256_file(cached) == hex_digest:
            print(f"[{i+1}/{len(descriptors)}] {digest} size={desc.get('size')} cached", flush=True)
            _link_or_copy(cached, dst)
        elif not dst.exists() or sha256_file(dst) != hex_digest:
            blob_url = f"{REGISTRY}/v2/{repo}/blobs/{digest}"
            print(f"[{i+1}/{len(descriptors)}] {digest} size={desc.get('size')}", flush=True)
            # Docker Hub bearer tokens can expire during large image downloads.
            # Refresh before each blob so a long previous layer does not poison
            # all following requests with 401s.
            curl_file(blob_url, dst, retries=16, auth_repo=repo)
            _link_or_copy(dst, cached)
        actual = sha256_file(dst)
        if actual != hex_digest:
            raise RuntimeError(f"digest mismatch for {digest}: got sha256:{actual}")

    (work / "oci-layout").write_text(json.dumps({"imageLayoutVersion": "1.0.0"}) + "\n")
    index = {
        "schemaVersion": 2,
        "manifests": [{
            "mediaType": media_type,
            "digest": f"sha256:{manifest_digest}",
            "size": (blobs / manifest_digest).stat().st_size,
            "annotations": {
                "org.opencontainers.image.ref.name": tag,
                "io.containerd.image.name": args.image,
            },
        }],
    }
    (work / "index.json").write_text(json.dumps(index, indent=2) + "\n")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    print(f"writing OCI archive {out}", flush=True)
    with tarfile.open(out, "w") as tar:
        for rel in ["oci-layout", "index.json"]:
            tar.add(work / rel, arcname=rel)
        for blob in sorted(blobs.iterdir()):
            tar.add(blob, arcname=f"blobs/sha256/{blob.name}")
    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    print(f"done: {out} ({out.stat().st_size} bytes)", flush=True)
    return 0


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def dockerhub_resolve_args() -> list[str]:
    """Return curl --resolve entries from public DNS when local DNS is bad.

    Some enterprise/VPN DNS setups occasionally resolve Docker Hub endpoints to
    unreachable addresses.  The OCI fallback is only used after docker pull has
    already failed, so it is reasonable to bypass local DNS for Docker Hub
    hosts while leaving the rest of the machine untouched.
    """
    global _RESOLVE_ARGS
    if os.environ.get("DOCKERHUB_USE_PUBLIC_DNS_RESOLVE", "0") != "1":
        return []
    if _RESOLVE_ARGS is not None:
        return _RESOLVE_ARGS
    args: list[str] = []
    for host in RESOLVE_HOSTS:
        ips = resolve_public_a_records(host)
        if not ips:
            continue
        for ip in ips[:4]:
            args.extend(["--resolve", f"{host}:443:{ip}"])
    if args:
        print("using public-DNS curl --resolve entries for Docker Hub fallback", flush=True)
    _RESOLVE_ARGS = args
    return args


def resolve_public_a_records(host: str) -> list[str]:
    for nameserver in ("1.1.1.1", "8.8.8.8"):
        proc = subprocess.run(
            ["dig", "+short", "A", host, f"@{nameserver}"],
            text=True,
            capture_output=True,
            check=False,
        )
        ips = [
            line.strip()
            for line in proc.stdout.splitlines()
            if line.strip() and all(part.isdigit() and 0 <= int(part) <= 255 for part in line.strip().split("."))
        ]
        if ips:
            return ips
    return []


if __name__ == "__main__":
    raise SystemExit(main())
