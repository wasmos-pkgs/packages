#!/usr/bin/env python3
"""Minimal zero-dependency OCI Distribution v2 client for `ghcr.io/wasmos-pkgs/*`.

Supports:
  - `push`: Push a `.tar.gz` or `.tar.zst` payload as an OCI v1.1 image to GHCR.
  - `pull`: Pull and verify an OCI v1.1 image layer from GHCR and extract it.
  - `publish-index`: Build and push `ghcr.io/wasmos-pkgs/index:latest` from `packages/*/pkg.toml`.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import gzip
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import toml_loader as tomllib  # noqa: E402

REGISTRY = os.environ.get("WASMOS_OCI_REGISTRY", "ghcr.io")
DEFAULT_ORG = os.environ.get("WASMOS_OCI_ORG", "wasmos-pkgs")
SOURCE_URL = "https://github.com/wasmos-pkgs/packages"

OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_GZIP_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
OCI_LAYER_ZSTD_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar+zstd"


def sha256_file(path: pathlib.Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            size += len(chunk)
    return f"sha256:{h.hexdigest()}", size


def uncompressed_sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    if path.name.endswith(".tar.gz") or path.name.endswith(".tgz"):
        with gzip.open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        return f"sha256:{h.hexdigest()}"
    if path.name.endswith(".tar.zst"):
        proc = subprocess.Popen(["zstd", "-dc", str(path)], stdout=subprocess.PIPE)
        assert proc.stdout is not None
        while chunk := proc.stdout.read(1 << 20):
            h.update(chunk)
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"zstd -dc failed with code {rc}")
        return f"sha256:{h.hexdigest()}"
    digest, _ = sha256_file(path)
    return digest


def get_bearer_token(repo: str, actions: str) -> str:
    token_env = os.environ.get("GHCR_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    user_env = os.environ.get("GHCR_USER") or os.environ.get("GITHUB_ACTOR") or "wasmos-pkgs"
    query = urllib.parse.urlencode({"service": REGISTRY, "scope": f"repository:{repo}:{actions}"})
    url = f"https://{REGISTRY}/token?{query}"
    req = urllib.request.Request(url)
    if token_env:
        basic = base64.b64encode(f"{user_env}:{token_env}".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", f"Basic {basic}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tok = data.get("token") or data.get("access_token")
    if not tok:
        raise RuntimeError(f"Registry token response missing token: {data}")
    return tok


def api_request(
    method: str,
    url: str,
    token: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, dict(e.headers.items()), body


def upload_blob_bytes(repo: str, token: str, payload: bytes) -> tuple[str, int]:
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    base = f"https://{REGISTRY}"
    status, _, _ = api_request("HEAD", f"{base}/v2/{repo}/blobs/{digest}", token)
    if status == 200:
        return digest, len(payload)

    status, headers, body = api_request("POST", f"{base}/v2/{repo}/blobs/uploads/", token)
    if status not in (200, 202):
        raise RuntimeError(f"Initiate blob upload failed ({status}): {body.decode('utf-8', 'replace')}")
    loc = headers.get("Location") or headers.get("location")
    if not loc:
        raise RuntimeError("Blob upload response missing Location header")
    upload_url = urllib.parse.urljoin(base, loc)
    sep = "&" if "?" in upload_url else "?"
    put_url = f"{upload_url}{sep}digest={urllib.parse.quote(digest, safe=':')}"

    status, _, body = api_request(
        "PUT",
        put_url,
        token,
        data=payload,
        headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(payload))},
    )
    if status not in (200, 201):
        raise RuntimeError(f"Blob PUT failed ({status}): {body.decode('utf-8', 'replace')}")
    return digest, len(payload)


def upload_blob_file(repo: str, token: str, path: pathlib.Path) -> tuple[str, int]:
    digest, size = sha256_file(path)
    base = f"https://{REGISTRY}"
    status, _, _ = api_request("HEAD", f"{base}/v2/{repo}/blobs/{digest}", token)
    if status == 200:
        print(f"Blob {digest[:19]}... already exists on {REGISTRY}", flush=True)
        return digest, size

    status, headers, body = api_request("POST", f"{base}/v2/{repo}/blobs/uploads/", token)
    if status not in (200, 202):
        raise RuntimeError(f"Initiate blob upload failed ({status}): {body.decode('utf-8', 'replace')}")
    loc = headers.get("Location") or headers.get("location")
    if not loc:
        raise RuntimeError("Blob upload response missing Location header")
    upload_url = urllib.parse.urljoin(base, loc)
    sep = "&" if "?" in upload_url else "?"
    put_url = f"{upload_url}{sep}digest={urllib.parse.quote(digest, safe=':')}"

    # Stream large files via curl so we don't buffer 100MB+ toolchains in memory.
    res = subprocess.run(
        [
            "curl",
            "-fsSL",
            "-X", "PUT",
            "-H", f"Authorization: Bearer {token}",
            "-H", "Content-Type: application/octet-stream",
            "-T", str(path),
            put_url,
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"curl blob upload failed: {res.stderr}")
    return digest, size


def push_oci_archive(
    repo: str,
    tags: list[str],
    archive: pathlib.Path,
    *,
    arch: str = "wasm64",
    os_name: str = "linux",
    annotations: dict[str, str] | None = None,
) -> str:
    token = get_bearer_token(repo, "pull,push")
    layer_media = (
        OCI_LAYER_ZSTD_MEDIA_TYPE
        if archive.name.endswith(".tar.zst")
        else OCI_LAYER_GZIP_MEDIA_TYPE
    )
    diff_id = uncompressed_sha256(archive)
    layer_digest, layer_size = upload_blob_file(repo, token, archive)

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    config_doc = {
        "created": now,
        "architecture": arch,
        "os": os_name,
        "config": {},
        "rootfs": {
            "type": "layers",
            "diff_ids": [diff_id],
        },
    }
    config_bytes = json.dumps(config_doc, separators=(",", ":")).encode("utf-8")
    config_digest, config_size = upload_blob_bytes(repo, token, config_bytes)

    manifest_annotations = {
        "org.opencontainers.image.source": SOURCE_URL,
        "org.opencontainers.image.created": now,
    }
    if annotations:
        manifest_annotations.update(annotations)

    manifest_doc = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST_MEDIA_TYPE,
        "config": {
            "mediaType": OCI_CONFIG_MEDIA_TYPE,
            "digest": config_digest,
            "size": config_size,
        },
        "layers": [
            {
                "mediaType": layer_media,
                "digest": layer_digest,
                "size": layer_size,
            }
        ],
        "annotations": manifest_annotations,
    }
    manifest_bytes = json.dumps(manifest_doc, separators=(",", ":")).encode("utf-8")
    manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"

    base = f"https://{REGISTRY}"
    for tag in tags:
        status, _, body = api_request(
            "PUT",
            f"{base}/v2/{repo}/manifests/{tag}",
            token,
            data=manifest_bytes,
            headers={"Content-Type": OCI_MANIFEST_MEDIA_TYPE},
        )
        if status not in (200, 201):
            raise RuntimeError(
                f"Manifest PUT {repo}:{tag} failed ({status}): {body.decode('utf-8', 'replace')}"
            )
        print(f"Pushed {REGISTRY}/{repo}:{tag} ({manifest_digest[:19]}..., {layer_size} bytes)", flush=True)
    return manifest_digest


def pull_oci_archive(repo: str, tag: str, dest: pathlib.Path, *, strip_usr: bool = False) -> None:
    token = get_bearer_token(repo, "pull")
    base = f"https://{REGISTRY}"
    status, _, body = api_request(
        "GET",
        f"{base}/v2/{repo}/manifests/{tag}",
        token,
        headers={"Accept": f"{OCI_MANIFEST_MEDIA_TYPE}, application/vnd.docker.distribution.manifest.v2+json"},
    )
    if status != 200:
        raise RuntimeError(
            f"Manifest GET {repo}:{tag} failed ({status}): {body.decode('utf-8', 'replace')}"
        )
    manifest = json.loads(body.decode("utf-8"))
    layers = manifest.get("layers", [])
    if not layers:
        raise RuntimeError(f"Manifest {repo}:{tag} contains no layers")

    dest.mkdir(parents=True, exist_ok=True)
    for layer in layers:
        digest = layer["digest"]
        media = layer.get("mediaType", "")
        is_zstd = "zstd" in media
        suffix = ".tar.zst" if is_zstd else ".tar.gz"
        tmp_blob = dest / f".oci-layer-{digest.split(':')[-1][:12]}{suffix}"
        try:
            res = subprocess.run(
                [
                    "curl",
                    "-fsSL",
                    "-H", f"Authorization: Bearer {token}",
                    "-o", str(tmp_blob),
                    f"{base}/v2/{repo}/blobs/{digest}",
                ],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                raise RuntimeError(f"curl blob download failed: {res.stderr}")
            actual, _ = sha256_file(tmp_blob)
            if actual != digest:
                raise RuntimeError(f"Digest mismatch for {repo}:{tag}: expected {digest}, got {actual}")

            tar_cmd = ["tar"]
            if is_zstd:
                tar_cmd.append("--zstd")
            else:
                tar_cmd.append("-z")
            tar_cmd.extend(["-xf", str(tmp_blob), "-C", str(dest)])
            if strip_usr:
                tar_cmd.extend(["--strip-components=1"])
            subprocess.run(tar_cmd, check=True)
        finally:
            if tmp_blob.exists():
                tmp_blob.unlink()
    print(f"Pulled {REGISTRY}/{repo}:{tag} -> {dest}", flush=True)


def cmd_publish_index(repo_root: pathlib.Path, org: str, dry_run: bool) -> None:
    pkgs_dir = repo_root / "packages"
    entries: list[dict[str, object]] = []
    for pkg_toml in sorted(pkgs_dir.glob("*/pkg.toml")):
        data = tomllib.loads(pkg_toml.read_text(encoding="utf-8"))
        pkg = data["package"]
        full_ver = f"{pkg['version']}-r{pkg.get('rev', 0)}"
        entries.append(
            {
                "name": pkg["name"],
                "version": pkg["version"],
                "rev": pkg.get("rev", 0),
                "full_version": full_ver,
                "kind": pkg.get("kind", "runtime"),
                "description": pkg.get("description", ""),
                "license": pkg.get("license", ""),
                "homepage": pkg.get("homepage", ""),
                "build_deps": pkg.get("build_deps", []),
                "runtime_deps": pkg.get("runtime_deps", []),
                "image": f"{REGISTRY}/{org}/{pkg['name']}:{full_ver}",
            }
        )

    index_doc = {
        "schema": 1,
        "arch": "wasm64",
        "os": "linux",
        "triple": "wasm64-submilli-linux-musl",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "packages": entries,
    }
    index_bytes = (json.dumps(index_doc, indent=2) + "\n").encode("utf-8")

    dist_dir = repo_root / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    out_tgz = dist_dir / "index.tar.gz"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        ti = tarfile.TarInfo(name="index.json")
        ti.size = len(index_bytes)
        ti.mode = 0o644
        ti.mtime = 1_700_000_000
        tf.addfile(ti, io.BytesIO(index_bytes))
    with gzip.GzipFile(filename="", mode="wb", fileobj=out_tgz.open("wb"), mtime=0) as gz:
        gz.write(raw.getvalue())

    print(f"Generated index ({len(entries)} packages) -> {out_tgz}", flush=True)
    if not dry_run:
        push_oci_archive(
            f"{org}/index",
            ["latest"],
            out_tgz,
            annotations={"org.opencontainers.image.description": "wasmos-pkgs package index"},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="OCI Distribution v2 helper for wasmos-pkgs")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_push = sub.add_parser("push")
    p_push.add_argument("--repo", required=True, help="Repository path, e.g. wasmos-pkgs/bash")
    p_push.add_argument("--tag", action="append", required=True, help="Tag(s) to push")
    p_push.add_argument("--file", type=pathlib.Path, required=True)
    p_push.add_argument("--arch", default="wasm64")
    p_push.add_argument("--os", dest="os_name", default="linux")
    p_push.add_argument("--annotation", action="append", default=[])

    p_pull = sub.add_parser("pull")
    p_pull.add_argument("--repo", required=True)
    p_pull.add_argument("--tag", required=True)
    p_pull.add_argument("--dest", type=pathlib.Path, required=True)
    p_pull.add_argument("--strip-usr", action="store_true")

    p_idx = sub.add_parser("publish-index")
    p_idx.add_argument("--org", default=DEFAULT_ORG)
    p_idx.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    if args.cmd == "push":
        ann: dict[str, str] = {}
        for item in args.annotation:
            k, v = item.split("=", 1)
            ann[k] = v
        push_oci_archive(
            args.repo,
            args.tag,
            args.file,
            arch=args.arch,
            os_name=args.os_name,
            annotations=ann,
        )
    elif args.cmd == "pull":
        pull_oci_archive(args.repo, args.tag, args.dest, strip_usr=args.strip_usr)
    elif args.cmd == "publish-index":
        cmd_publish_index(pathlib.Path(__file__).resolve().parents[1], args.org, args.dry_run)


if __name__ == "__main__":
    main()
