#!/usr/bin/env python3
"""Build, verify, package, and optionally publish a single `wasmos-pkgs` package."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import oci  # noqa: E402
import toml_loader as tomllib  # noqa: E402


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def fetch_source(pkg_name: str, src_cfg: dict[str, object], src_dir: pathlib.Path, cache_dir: pathlib.Path) -> None:
    if src_dir.exists():
        shutil.rmtree(src_dir)
    src_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if "url" in src_cfg:
        url = str(src_cfg["url"])
        expected_sha = str(src_cfg["sha256"])
        filename = url.rsplit("/", 1)[-1]
        archive = cache_dir / filename
        if not archive.exists() or sha256_file(archive) != expected_sha:
            print(f"Downloading {url} -> {archive}", flush=True)
            urllib.request.urlretrieve(url, archive)
        actual_sha = sha256_file(archive)
        if actual_sha != expected_sha:
            raise SystemExit(f"SHA-256 mismatch for {filename}: expected {expected_sha}, got {actual_sha}")

        strip = int(src_cfg.get("strip_components", 1))
        cmd = ["tar", "-xf", str(archive), "-C", str(src_dir)]
        if strip > 0:
            cmd.append(f"--strip-components={strip}")
        subprocess.run(cmd, check=True)
    elif "git" in src_cfg:
        repo = str(src_cfg["git"])
        rev = str(src_cfg["rev"])
        branch = str(src_cfg.get("branch", "master"))
        subprocess.run(["git", "init", str(src_dir)], check=True)
        subprocess.run(["git", "-C", str(src_dir), "remote", "add", "origin", repo], check=True)
        # Try direct commit fetch first; fall back to shallow branch fetch if remote disallows fetch-by-sha.
        res = subprocess.run(["git", "-C", str(src_dir), "fetch", "--depth", "1", "origin", rev], check=False)
        if res.returncode != 0:
            subprocess.run(["git", "-C", str(src_dir), "fetch", "--depth", "200", "origin", branch], check=True)
        subprocess.run(["git", "-C", str(src_dir), "checkout", "--detach", rev], check=True)
    elif src_cfg.get("builtin"):
        return
    else:
        raise SystemExit(f"Package '{pkg_name}' [source] must specify 'url', 'git', or 'builtin = true'")


def install_build_deps(
    build_deps: list[str],
    sysroot: pathlib.Path,
    org: str,
    local_dist: pathlib.Path | None,
) -> None:
    for dep in build_deps:
        dep_toml = REPO_ROOT / "packages" / dep / "pkg.toml"
        dep_cfg = tomllib.loads(dep_toml.read_text(encoding="utf-8"))["package"]
        full_ver = f"{dep_cfg['version']}-r{dep_cfg.get('rev', 0)}"

        # Prefer locally built artifact from an earlier wave in the same CI run (e.g. PR builds).
        local_archive = (
            local_dist / f"{dep}-{full_ver}.tar.gz" if local_dist is not None else None
        )
        if local_archive and local_archive.is_file():
            print(f"Installing build_dep '{dep}' from local artifact {local_archive}", flush=True)
            subprocess.run(
                ["tar", "-xzf", str(local_archive), "-C", str(sysroot), "--strip-components=1", "usr"],
                check=True,
            )
        else:
            print(f"Pulling build_dep '{dep}' ({org}/{dep}:{full_ver}) into {sysroot}", flush=True)
            oci.pull_oci_archive(f"{org}/{dep}", full_ver, sysroot, strip_usr=True)


def verify_outputs(pkg: dict[str, object], destdir: pathlib.Path) -> list[str]:
    files: list[str] = []
    kind = str(pkg.get("kind", "runtime"))
    wasm_found = False

    for p in sorted(destdir.rglob("*")):
        if p.is_dir() and not p.is_symlink():
            continue
        rel = "/" + p.relative_to(destdir).as_posix()
        files.append(rel)
        if p.is_file() and not p.is_symlink() and os.access(p, os.X_OK):
            magic = p.read_bytes()[:4]
            if magic == b"\x00asm":
                wasm_found = True
            elif magic.startswith(b"#!"):
                pass
            else:
                raise SystemExit(f"Executable {rel} is not a Wasm binary or script (magic={magic!r})")

    if kind == "runtime" and not wasm_found:
        raise SystemExit(f"Runtime package '{pkg['name']}' produced no executable Wasm binaries")
    if not files:
        raise SystemExit(f"Package '{pkg['name']}' produced no files in {destdir}")
    return files


def create_reproducible_tgz(destdir: pathlib.Path, out_tgz: pathlib.Path) -> None:
    out_tgz.parent.mkdir(parents=True, exist_ok=True)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        for p in sorted(destdir.rglob("*")):
            arcname = p.relative_to(destdir).as_posix()
            ti = tf.gettarinfo(str(p), arcname=arcname)
            ti.uid = 0
            ti.gid = 0
            ti.uname = "root"
            ti.gname = "root"
            ti.mtime = 1_700_000_000
            if ti.isfile():
                with p.open("rb") as f:
                    tf.addfile(ti, f)
            else:
                tf.addfile(ti)
    with gzip.GzipFile(filename="", mode="wb", fileobj=out_tgz.open("wb"), mtime=0) as gz:
        gz.write(raw.getvalue())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", help="Package directory name under packages/")
    parser.add_argument("--toolchain", type=pathlib.Path, required=True)
    parser.add_argument("--org", default=oci.DEFAULT_ORG)
    parser.add_argument("--work-dir", type=pathlib.Path, default=REPO_ROOT / "build/pkgs")
    parser.add_argument("--dist-dir", type=pathlib.Path, default=REPO_ROOT / "dist")
    parser.add_argument("--deps-dir", type=pathlib.Path, default=None, help="Optional directory of locally built dep tarballs")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--push", action="store_true", help="Push built OCI package to GHCR")
    args = parser.parse_args()

    pkg_dir = REPO_ROOT / "packages" / args.package
    toml_path = pkg_dir / "pkg.toml"
    build_script = pkg_dir / "build.py"
    if not toml_path.is_file() or not build_script.is_file():
        raise SystemExit(f"Missing pkg.toml or build.py in {pkg_dir}")

    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    pkg = data["package"]
    src_cfg = data["source"]

    name = str(pkg["name"])
    version = str(pkg["version"])
    rev = int(pkg.get("rev", 0))
    full_ver = f"{version}-r{rev}"

    toolchain = args.toolchain.resolve()
    sysroot = toolchain / "sysroot"
    work = args.work_dir.resolve() / name
    src_dir = work / "src"
    build_dir = work / "build"
    destdir = work / "destdir"
    cache_dir = args.work_dir.resolve() / "_downloads"

    if build_dir.exists():
        shutil.rmtree(build_dir)
    if destdir.exists():
        shutil.rmtree(destdir)
    build_dir.mkdir(parents=True, exist_ok=True)
    destdir.mkdir(parents=True, exist_ok=True)

    install_build_deps(pkg.get("build_deps", []), sysroot, args.org, args.deps_dir)
    fetch_source(name, src_cfg, src_dir, cache_dir)

    env = os.environ.copy()
    env["WASMOS_TOOLCHAIN"] = str(toolchain)
    env["WASMOS_SYSROOT"] = str(sysroot)
    env["WASMOS_SRC"] = str(src_dir)
    env["WASMOS_BUILD"] = str(build_dir)
    env["WASMOS_DESTDIR"] = str(destdir)
    env["WASMOS_JOBS"] = str(args.jobs)

    print(f"=== Building {name} {full_ver} ===", flush=True)
    subprocess.run([sys.executable, str(build_script)], env=env, check=True)

    files = verify_outputs(pkg, destdir)
    meta = {
        "name": name,
        "version": version,
        "rev": rev,
        "full_version": full_ver,
        "kind": pkg.get("kind", "runtime"),
        "description": pkg.get("description", ""),
        "license": pkg.get("license", ""),
        "homepage": pkg.get("homepage", ""),
        "build_deps": pkg.get("build_deps", []),
        "runtime_deps": pkg.get("runtime_deps", []),
        "files": files,
    }
    meta_path = destdir / "usr/share/wasmos-pkg" / f"{name}.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    out_tgz = args.dist_dir.resolve() / f"{name}-{full_ver}.tar.gz"
    create_reproducible_tgz(destdir, out_tgz)
    print(f"Packaged {name} {full_ver} -> {out_tgz} ({out_tgz.stat().st_size} bytes)", flush=True)

    if args.push:
        tags = [full_ver, version, "latest"]
        oci.push_oci_archive(
            f"{args.org}/{name}",
            tags,
            out_tgz,
            arch="wasm64",
            os_name="linux",
            annotations={
                "org.opencontainers.image.version": full_ver,
                "org.opencontainers.image.description": str(pkg.get("description", "")),
                "org.opencontainers.image.licenses": str(pkg.get("license", "")),
                "org.wasmos.pkg.name": name,
                "org.wasmos.pkg.kind": str(pkg.get("kind", "runtime")),
            },
        )


if __name__ == "__main__":
    main()
