#!/usr/bin/env python3
"""Package `pip`, `setuptools`, `wheel`, `packaging`, pure-Python `ctypes` shim,
and `/etc/pip.conf` (`extra-index-url = https://wasmos-pkgs.github.io/wheels/simple/`,
`prefer-binary = true`) so `wpkg install pip` on any WasmOS base image works out of the box.
"""

from __future__ import annotations

import json
import os
import pathlib
import urllib.request
import zipfile

DESTDIR = pathlib.Path(os.environ["WASMOS_DESTDIR"])
BUILD = pathlib.Path(os.environ["WASMOS_BUILD"])
WHEELS_INDEX_URL = "https://wasmos-pkgs.github.io/wheels/simple/"
BOOTSTRAP_WHEELS = ["pip", "setuptools", "wheel", "packaging"]

CTYPES_INIT_PY = """\"\"\"Pure-Python `ctypes` compatibility shim for `wasm64-submilli-linux-musl`.\"\"\"
from __future__ import annotations

RTLD_LOCAL = 0
RTLD_GLOBAL = 256
DEFAULT_MODE = RTLD_LOCAL
_last_errno = 0

def get_errno() -> int: return _last_errno
def set_errno(val: int) -> int:
    global _last_errno
    old = _last_errno; _last_errno = int(val); return old

class ArgumentError(Exception): pass
class _ByRef:
    def __init__(self, obj, offset: int = 0): self._obj = obj; self._offset = offset
class _CDataMeta(type):
    def __mul__(cls, length: int): return _make_array_type(cls, int(length))
class _CData(metaclass=_CDataMeta):
    _size_ = 8; _align_ = 8
    @classmethod
    def in_dll(cls, dll, name: str): raise OSError(f"symbol {name!r} unavailable without libffi CDLL")
class _SimpleCData(_CData):
    _type_ = "P"; _size_ = 8; _align_ = 8
    def __init__(self, value=None):
        if value is None:
            self.value = None if self._type_ in ("z", "Z", "P") else (0.0 if self._type_ in ("f", "d", "g") else (False if self._type_ == "?" else (b"\\x00" if self._type_ == "c" else ("\\x00" if self._type_ == "u" else 0))))
        else:
            self.value = value
def _scalar(name: str, code: str, size: int):
    return _CDataMeta(name, (_SimpleCData,), {"_type_": code, "_size_": size, "_align_": min(size, 8)})

c_bool = _scalar("c_bool", "?", 1); c_char = _scalar("c_char", "c", 1); c_wchar = _scalar("c_wchar", "u", 4)
c_byte = _scalar("c_byte", "b", 1); c_ubyte = _scalar("c_ubyte", "B", 1); c_short = _scalar("c_short", "h", 2)
c_ushort = _scalar("c_ushort", "H", 2); c_int = _scalar("c_int", "i", 4); c_uint = _scalar("c_uint", "I", 4)
c_long = _scalar("c_long", "l", 8); c_ulong = _scalar("c_ulong", "L", 8); c_longlong = _scalar("c_longlong", "q", 8)
c_ulonglong = _scalar("c_ulonglong", "Q", 8); c_size_t = c_ulong; c_ssize_t = c_long
c_float = _scalar("c_float", "f", 4); c_double = _scalar("c_double", "d", 8); c_longdouble = _scalar("c_longdouble", "g", 16)
c_char_p = _scalar("c_char_p", "z", 8); c_wchar_p = _scalar("c_wchar_p", "Z", 8); c_void_p = _scalar("c_void_p", "P", 8)
c_voidp = c_void_p; py_object = _scalar("py_object", "O", 8)
c_int8 = c_byte; c_uint8 = c_ubyte; c_int16 = c_short; c_uint16 = c_ushort
c_int32 = c_int; c_uint32 = c_uint; c_int64 = c_longlong; c_uint64 = c_ulonglong

class Array(_CData):
    _type_ = c_char; _length_ = 0
    def __init__(self, *args): self._items = list(args); self._raw_buf = bytearray(sizeof(type(self)))
    def __len__(self) -> int: return self._length_
_ARRAY_CACHE: dict[tuple[type, int], type[Array]] = {}
def _make_array_type(elem_type: type, length: int) -> type[Array]:
    key = (elem_type, length)
    if key not in _ARRAY_CACHE:
        _ARRAY_CACHE[key] = _CDataMeta(f"{elem_type.__name__}_Array_{length}", (Array,), {"_type_": elem_type, "_length_": length, "_size_": sizeof(elem_type) * max(0, length), "_align_": alignment(elem_type)})
    return _ARRAY_CACHE[key]
class Structure(_CData):
    _size_ = 8; _align_ = 8
    def __init__(self, *args, **kwargs):
        for idx, item in enumerate(getattr(self, "_fields_", ())):
            setattr(self, item[0], args[idx] if idx < len(args) else kwargs.get(item[0], 0))
BigEndianStructure = Structure; LittleEndianStructure = Structure; Union = Structure
class _Pointer(_CData):
    _type_ = c_void_p; _size_ = 8; _align_ = 8
    def __init__(self, contents=None): self.contents = contents
_PTR_CACHE: dict[object, type[_Pointer]] = {}
def POINTER(tp):
    if tp is None: return c_void_p
    if tp not in _PTR_CACHE:
        _PTR_CACHE[tp] = _CDataMeta(f"LP_{getattr(tp, '__name__', repr(tp))}", (_Pointer,), {"_type_": tp, "_size_": 8, "_align_": 8})
    return _PTR_CACHE[tp]
def pointer(obj): return POINTER(type(obj))(obj)
def byref(obj, offset: int = 0): return _ByRef(obj, offset)
def sizeof(tp_or_obj) -> int:
    tp = tp_or_obj if isinstance(tp_or_obj, type) else type(tp_or_obj)
    return int(getattr(tp, "_size_", 8))
def alignment(tp_or_obj) -> int:
    tp = tp_or_obj if isinstance(tp_or_obj, type) else type(tp_or_obj)
    return int(getattr(tp, "_align_", 8))
def addressof(obj) -> int: return id(obj)
def cast(obj, typ): return c_void_p(id(obj)) if typ is c_void_p else (typ(obj) if isinstance(typ, type) and issubclass(typ, _Pointer) else obj)
def create_string_buffer(init, size: int | None = None):
    n = (len(init) + 1 if size is None else int(size)) if isinstance(init, (bytes, bytearray)) else int(init)
    return (c_char * n)()
def create_unicode_buffer(init, size: int | None = None):
    n = (len(init) + 1 if size is None else int(size)) if isinstance(init, str) else int(init)
    return (c_wchar * n)()
def CFUNCTYPE(restype, *argtypes, **kwargs):
    class _CFunc(_CData):
        def __init__(self, func=None): self._func = func
    return _CFunc
PYFUNCTYPE = CFUNCTYPE
class CDLL:
    def __init__(self, name=None, mode: int = DEFAULT_MODE, handle=None, use_errno: bool = False, use_last_error: bool = False, winmode=None):
        raise OSError(f"CDLL({name!r}) unavailable without libffi on wasm64-submilli-linux-musl")
PyDLL = CDLL; WinDLL = CDLL; OleDLL = CDLL
class LibraryLoader:
    def __init__(self, dlltype): self._dlltype = dlltype
    def __getattr__(self, name: str):
        if name.startswith("_"): raise AttributeError(name)
        return self._dlltype(name)
    def LoadLibrary(self, name: str): return self._dlltype(name)
cdll = LibraryLoader(CDLL); pydll = LibraryLoader(PyDLL)
"""

CTYPES_UTIL_PY = "def find_library(name: str):\n    return None\n\ndef find_msvcrt():\n    return None\n"
CTYPES_WINTYPES_PY = (
    "from . import POINTER, c_bool, c_byte, c_char_p, c_int, c_long, c_longlong, c_short, c_size_t, "
    "c_ubyte, c_uint, c_ulong, c_ulonglong, c_ushort, c_void_p, c_wchar, c_wchar_p\n"
    "BYTE = c_ubyte; WORD = c_ushort; DWORD = c_ulong; BOOL = c_int; BOOLEAN = BYTE; SHORT = c_short; "
    "USHORT = c_ushort; INT = c_int; UINT = c_uint; LONG = c_long; ULONG = c_ulong; LARGE_INTEGER = c_longlong; "
    "ULARGE_INTEGER = c_ulonglong; HANDLE = c_void_p; HWND = HANDLE; HMODULE = HANDLE; HINSTANCE = HANDLE; "
    "HKEY = HANDLE; LPVOID = c_void_p; LPCVOID = c_void_p; LPWSTR = c_wchar_p; LPCWSTR = c_wchar_p; "
    "LPSTR = c_char_p; LPCSTR = c_char_p; WCHAR = c_wchar; SIZE_T = c_size_t; LPDWORD = POINTER(DWORD); LPBOOL = POINTER(BOOL)\n"
)


def fetch_pure_wheel(pypi_pkg: str) -> pathlib.Path:
    req = urllib.request.Request(
        f"https://pypi.org/pypi/{pypi_pkg}/json",
        headers={"User-Agent": "wasmos-pip-pkg/1.0"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    wheels = [u for u in data.get("urls", []) if u.get("filename", "").endswith("py3-none-any.whl")]
    if not wheels:
        raise SystemExit(f"No py3-none-any.whl found for {pypi_pkg}")
    w = wheels[0]
    dst = BUILD / w["filename"]
    print(f"Downloading {w['filename']}...", flush=True)
    wreq = urllib.request.Request(w["url"], headers={"User-Agent": "wasmos-pip-pkg/1.0"})
    with urllib.request.urlopen(wreq, timeout=120) as wresp:
        dst.write_bytes(wresp.read())
    return dst


def extract_wheel(whl_path: pathlib.Path, site_packages: pathlib.Path) -> None:
    with zipfile.ZipFile(whl_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            parts = member.filename.split("/")
            if len(parts) >= 3 and parts[0].endswith(".data"):
                if parts[1] in ("purelib", "platlib"):
                    dst = site_packages / "/".join(parts[2:])
                else:
                    continue
            else:
                dst = site_packages / member.filename
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(zf.read(member))
            dst.chmod(0o644)


def write_script(path: pathlib.Path, module: str, func: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!/usr/bin/python3\nimport re, sys\nfrom {module} import {func}\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        f"    sys.exit({func}())\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def main() -> None:
    site_packages = DESTDIR / "usr/lib/python3.13/site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    for bpkg in BOOTSTRAP_WHEELS:
        whl = fetch_pure_wheel(bpkg)
        extract_wheel(whl, site_packages)

    for pip_name in ("pip", "pip3", "pip3.13"):
        write_script(DESTDIR / "usr/bin" / pip_name, "pip._internal.cli.main", "main")
    write_script(DESTDIR / "usr/bin/wheel", "wheel.cli", "main")

    ctypes_dir = DESTDIR / "usr/lib/python3.13/ctypes"
    ctypes_dir.mkdir(parents=True, exist_ok=True)
    for fname, body in [
        ("__init__.py", CTYPES_INIT_PY),
        ("util.py", CTYPES_UTIL_PY),
        ("wintypes.py", CTYPES_WINTYPES_PY),
        ("_endian.py", "from . import BigEndianStructure, LittleEndianStructure\n"),
    ]:
        f = ctypes_dir / fname
        f.write_text(body, encoding="utf-8")
        f.chmod(0o644)

    pip_conf = DESTDIR / "etc/pip.conf"
    pip_conf.parent.mkdir(parents=True, exist_ok=True)
    pip_conf.write_text(
        f"[global]\nextra-index-url = {WHEELS_INDEX_URL}\nprefer-binary = true\n"
        "disable-pip-version-check = true\nroot-user-action = ignore\n",
        encoding="utf-8",
    )
    pip_conf.chmod(0o644)
    print("Built pip package with /etc/pip.conf ->", DESTDIR, flush=True)


if __name__ == "__main__":
    main()
