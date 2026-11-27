"""TOML loader using stdlib `tomllib` on Python 3.11+, with a minimal fallback for 3.9/3.10."""

from __future__ import annotations

import ast
import pathlib

try:
    import tomllib as _tomllib
except ModuleNotFoundError:
    _tomllib = None


def loads(text: str) -> dict[str, object]:
    if _tomllib is not None:
        return _tomllib.loads(text)

    root: dict[str, object] = {}
    current: dict[str, object] = root
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            sec = line[1:-1].strip()
            current = {}
            root[sec] = current
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if val == "true":
            parsed: object = True
        elif val == "false":
            parsed = False
        else:
            parsed = ast.literal_eval(val)
        current[key] = parsed
    return root


def load_path(path: pathlib.Path) -> dict[str, object]:
    return loads(path.read_text(encoding="utf-8"))
