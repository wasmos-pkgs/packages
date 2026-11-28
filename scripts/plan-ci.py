#!/usr/bin/env python3
"""Compute topological package build waves for GitHub Actions CI.

Detects:
  - `wave1..wave4`: changed packages under `packages/<name>/**` expanded transitively across
    static-linking `build_deps`, grouped into parallel DAG waves.
  - Changes to `toolchain.toml` or core build scripts (`scripts/{build-pkg,fetch-toolchain,cargo-wasmos,oci,toml_loader}.py`)
    rebuild all packages.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import json
import os
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import toml_loader as tomllib  # noqa: E402

PACKAGES_DIR = REPO_ROOT / "packages"
MAX_WAVES = 4

GLOBAL_REBUILD_FILES = {
    "toolchain.toml",
    "scripts/build-pkg.py",
    "scripts/fetch-toolchain.py",
    "scripts/cargo-wasmos.py",
    "scripts/oci.py",
    "scripts/toml_loader.py",
}


def load_packages() -> dict[str, dict[str, object]]:
    pkgs: dict[str, dict[str, object]] = {}
    for toml_path in sorted(PACKAGES_DIR.glob("*/pkg.toml")):
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        pkg = data["package"]
        name = pkg["name"]
        if name != toml_path.parent.name:
            raise SystemExit(
                f"Package name mismatch: {toml_path} declares '{name}', dir is '{toml_path.parent.name}'"
            )
        pkgs[name] = pkg
    return pkgs


def changed_files(base: str, head: str) -> list[str]:
    if not base or set(base) == {"0"}:
        return [f"packages/{p.parent.name}/pkg.toml" for p in PACKAGES_DIR.glob("*/pkg.toml")]
    out = subprocess.check_output(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        cwd=REPO_ROOT,
        text=True,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def compute_waves(all_pkgs: dict[str, dict[str, object]], seeds: set[str]) -> list[list[str]]:
    if not seeds:
        return []

    dependents: dict[str, set[str]] = defaultdict(set)
    for name, pkg in all_pkgs.items():
        for dep in pkg.get("build_deps", []):
            if dep not in all_pkgs:
                raise SystemExit(f"Package '{name}' depends on unknown package '{dep}'")
            dependents[dep].add(name)

    selected: set[str] = set()
    q = deque(sorted(seeds))
    while q:
        cur = q.popleft()
        if cur in selected:
            continue
        selected.add(cur)
        for nxt in sorted(dependents.get(cur, ())):
            if nxt not in selected:
                q.append(nxt)

    indeg: dict[str, int] = {}
    for name in selected:
        deps = [d for d in all_pkgs[name].get("build_deps", []) if d in selected]
        indeg[name] = len(deps)

    waves: list[list[str]] = []
    remaining = set(selected)
    while remaining:
        wave = sorted(n for n in remaining if indeg[n] == 0)
        if not wave:
            raise SystemExit(f"Dependency cycle detected among: {sorted(remaining)}")
        waves.append(wave)
        for n in wave:
            remaining.remove(n)
            for nxt in dependents.get(n, ()):
                if nxt in remaining:
                    indeg[nxt] -= 1

    if len(waves) > MAX_WAVES:
        raise SystemExit(f"Dependency depth {len(waves)} exceeds MAX_WAVES={MAX_WAVES}")
    return waves


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="", help="Base git SHA for diff")
    parser.add_argument("--head", default="HEAD", help="Head git SHA for diff")
    parser.add_argument("--packages", default="", help="Explicit comma-separated package names, 'all', or ''")
    parser.add_argument("--github-output", type=pathlib.Path, default=None)
    args = parser.parse_args()

    all_pkgs = load_packages()
    seeds: set[str] = set()

    explicit = args.packages.strip()
    if explicit:
        if explicit.lower() == "all":
            seeds = set(all_pkgs.keys())
        elif explicit.lower() != "none":
            for item in explicit.split(","):
                name = item.strip()
                if not name:
                    continue
                if name not in all_pkgs:
                    raise SystemExit(f"Unknown package requested: '{name}'")
                seeds.add(name)
    else:
        files = changed_files(args.base, args.head)
        rebuild_all_pkgs = False
        for f in files:
            p = pathlib.PurePosixPath(f)
            parts = p.parts
            if p.suffix == ".md":
                continue
            if len(parts) >= 2 and parts[0] == "packages" and parts[1] in all_pkgs:
                seeds.add(parts[1])
            elif f in GLOBAL_REBUILD_FILES:
                rebuild_all_pkgs = True
        if rebuild_all_pkgs:
            seeds = set(all_pkgs.keys())

    waves = compute_waves(all_pkgs, seeds)
    result = {
        "has_package_changes": bool(waves),
        "has_changes": bool(waves),
        "total_packages": sum(len(w) for w in waves),
        "waves": waves,
    }
    print(json.dumps(result, indent=2))

    gh_out = args.github_output or (
        pathlib.Path(os.environ["GITHUB_OUTPUT"]) if "GITHUB_OUTPUT" in os.environ else None
    )
    if gh_out:
        with gh_out.open("a", encoding="utf-8") as f:
            f.write(f"has_package_changes={'true' if waves else 'false'}\n")
            f.write(f"has_changes={'true' if waves else 'false'}\n")
            for idx in range(MAX_WAVES):
                wave_list = waves[idx] if idx < len(waves) else []
                f.write(f"wave{idx + 1}={json.dumps(wave_list)}\n")


if __name__ == "__main__":
    main()
