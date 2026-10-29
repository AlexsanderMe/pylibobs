"""
Platform-aware libobs shared library locator.

Resolution order:
  1. LIBOBS_PATH environment variable (override for development)
  2. Bundled lib in pylibobs/_libs/<platform>/<arch>/
  3. ctypes.util.find_library("obs")
  4. Well-known OBS Studio install paths
"""
from __future__ import annotations    # lazy-evaluated annotations (Py3.9 compat)

import os
import platform
from pathlib import Path

_PACKAGE_DIR = Path(__file__).parent
_LIBS_DIR = _PACKAGE_DIR / "_libs"

_PLATFORM_LIB_NAMES = {
    "Windows": "obs.dll",
    "Linux": "libobs.so.0",
    "Darwin": "libobs.dylib",
}

_PLATFORM_SUBDIR = {
    "Windows": "windows",
    "Linux": "linux",
    "Darwin": "macos",
}

_ARCH_ALIASES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
}

_SYSTEM_FALLBACK_PATHS = {
    "Windows": [
        r"C:\Program Files\obs-studio\bin\64bit\obs.dll",
        r"C:\Program Files (x86)\obs-studio\bin\64bit\obs.dll",
    ],
    "Linux": [
        "/usr/lib/x86_64-linux-gnu/libobs.so.0",
        "/usr/lib/aarch64-linux-gnu/libobs.so.0",
        "/usr/lib/libobs.so.0",
        "/usr/local/lib/libobs.so.0",
    ],
    "Darwin": [
        "/Applications/OBS.app/Contents/Frameworks/libobs.framework/Versions/A/libobs",
        "/Applications/OBS.app/Contents/Frameworks/libobs.0.dylib",
        "/usr/local/lib/libobs.dylib",
        "/opt/homebrew/lib/libobs.dylib",
    ],
}


def _env_override() -> str | None:
    return os.environ.get("LIBOBS_PATH") or None


def _bundled_path() -> str | None:
    """Locate the bundled libobs shared library inside the wheel.

    Layout per platform:
      windows/<arch>/obs.dll            (alongside zlib, w32-pthreads, etc.)
      linux/<arch>/libobs.so.0          (plus obs-plugins/ and data/ siblings)
      macos/<arch>/Frameworks/libobs.framework/Versions/A/libobs
                              (@rpath deps live alongside it in Frameworks/)
    """
    system = platform.system()
    machine = platform.machine().lower()
    arch = _ARCH_ALIASES.get(machine, machine)
    subdir = _PLATFORM_SUBDIR.get(system)
    lib_name = _PLATFORM_LIB_NAMES.get(system)
    if not subdir or not lib_name:
        return None

    base = _LIBS_DIR / subdir / arch
    # Try the most-likely path first per platform
    candidates = []
    if system == "Darwin":
        candidates.append(base / "Frameworks/libobs.framework/Versions/A/libobs")
    candidates.extend([
        base / lib_name,                          # win/linux flat layout
        base / "Frameworks" / lib_name,           # macOS .app layout
        base / "Frameworks" / "libobs.0.dylib",   # macOS versioned alias
    ])
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _system_path() -> str | None:
    from ctypes.util import find_library

    system = platform.system()
    name = find_library("obs")
    if name:
        p = Path(name)
        if p.exists():
            return str(p)

    for path in _SYSTEM_FALLBACK_PATHS.get(system, []):
        if Path(path).exists():
            return path
    return None


def find_libobs() -> str:
    """Return the path to the libobs shared library, or raise ImportError."""
    path = _env_override() or _bundled_path() or _system_path()
    if not path:
        system = platform.system()
        msg = (
            "libobs shared library not found.\n\n"
            "Options:\n"
            "  1. Install OBS Studio: https://obsproject.com/download\n"
            "  2. Bundle libobs: python scripts/fetch_libs.py\n"
            f"  3. Set LIBOBS_PATH to the full path of the library "
            f"({_PLATFORM_LIB_NAMES.get(system, 'libobs shared lib')})\n"
        )
        raise ImportError(msg)
    return path


# OBS helpers that libobs spawns via GetModuleFileNameA(NULL)/<helper>.exe —
# they must sit next to the host EXE (python.exe), not next to obs.dll.
_OBS_HELPER_EXES_WINDOWS = [
    "obs-ffmpeg-mux.exe",   # ffmpeg_muxer output (recording)
    "obs-amf-test.exe",     # AMD encoder probe
    "obs-nvenc-test.exe",   # NVENC probe
    "obs-qsv-test.exe",     # QuickSync probe
]


def _get_host_module_dir() -> Path | None:
    """
    Return the directory of the actual host module on Windows.

    libobs uses GetModuleFileNameW(NULL) to locate helper exes. In a venv,
    this returns the BASE python.exe (e.g. C:\\Python312\\python.exe), not
    the venv launcher. So we must stage helpers there.
    """
    if platform.system() != "Windows":
        return None
    import ctypes
    from ctypes import wintypes

    GetModuleFileNameW = ctypes.windll.kernel32.GetModuleFileNameW
    GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    GetModuleFileNameW.restype = wintypes.DWORD
    buf = ctypes.create_unicode_buffer(1024)
    n = GetModuleFileNameW(None, buf, 1024)
    if n == 0:
        return None
    return Path(buf.value).parent


def _stage_helpers_next_to_host(lib_dir: Path) -> None:
    """Copy OBS helper EXEs to the directory of the actual host EXE."""
    if platform.system() != "Windows":
        return
    import shutil

    host_dir = _get_host_module_dir()
    if host_dir is None:
        return

    for name in _OBS_HELPER_EXES_WINDOWS:
        src = lib_dir / name
        if not src.exists():
            continue
        dst = host_dir / name
        try:
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                continue  # already staged
            shutil.copy2(src, dst)
        except (PermissionError, OSError):
            pass  # best-effort; user may not have write access


def prepare_dll_search_path(libobs_path: str) -> None:
    """
    On Windows, libobs.dll has many dependency DLLs in the same folder.
    Add that folder to the DLL search path so they can be resolved.

    Also stages OBS helper executables next to sys.executable, since libobs
    locates them via GetModuleFileNameA(NULL).

    No-op on Linux/macOS (those use rpath / standard ld search).
    """
    if platform.system() != "Windows":
        return

    lib_dir = Path(libobs_path).parent
    lib_dir_str = str(lib_dir)

    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(lib_dir_str)
        except (FileNotFoundError, OSError):
            pass

    cur = os.environ.get("PATH", "")
    if lib_dir_str not in cur.split(os.pathsep):
        os.environ["PATH"] = lib_dir_str + os.pathsep + cur

    _stage_helpers_next_to_host(lib_dir)


def _bundled_root(libobs_path: Path) -> Path | None:
    """Find the platform/architecture root above a bundled file or directory,
    including the libobs framework executable."""
    path = libobs_path.resolve()
    for parent in (path, *path.parents):
        if parent.parent.parent == _LIBS_DIR.resolve():
            return parent
    return None


def get_graphics_module() -> str:
    """Resolve the macOS renderer beside the loaded framework, or its OS name."""
    system = platform.system()
    if system == "Darwin":
        try:
            libobs_path = Path(find_libobs()).resolve()
        except ImportError:
            return "libobs-opengl"
        for parent in libobs_path.parents:
            if parent.name == "Frameworks":
                candidate = parent / "libobs-opengl.dylib"
                if candidate.is_file():
                    return str(candidate)
                break
    return "libobs-d3d11" if system == "Windows" else "libobs-opengl"


def get_obs_data_dir() -> str | None:
    """
    Return the OBS data directory (containing libobs/ shader effects),
    or None if not found.

    Resolution order:
      1. OBS_DATA_PATH env var
      2. Bundled `_libs/<plat>/<arch>/data` (if present)
      3. Bundled `_libs/<plat>/<arch>/../../data` (legacy layout)
      4. OS-standard OBS install paths
    """
    env = os.environ.get("OBS_DATA_PATH")
    if env and Path(env).exists():
        return env

    try:
        libobs_path = Path(find_libobs())
    except ImportError:
        return None

    bundled_root = _bundled_root(libobs_path)
    candidates = [bundled_root / "data"] if bundled_root is not None else []
    candidates.extend([
        libobs_path.parent / "data",
        libobs_path.parent.parent.parent / "data",  # OBS install layout
    ])
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    system = platform.system()
    if system == "Windows":
        candidates = [
            Path(r"C:\Program Files\obs-studio\data"),
        ]
    elif system == "Linux":
        candidates = [Path("/usr/share/obs"), Path("/usr/local/share/obs")]
    elif system == "Darwin":
        candidates = [Path("/Applications/OBS.app/Contents/Resources/data")]
    else:
        candidates = []

    for c in candidates:
        if c.exists():
            return str(c)
    return None


#: Qt frontend plugins shipped with OBS Studio. They build Qt widgets and abort
#: the process without a QApplication, which pylibobs never creates, so they
#: are never loaded from a system install (bundles trim them already).
QT_FRONTEND_MODULES = _QT_FRONTEND_MODULES = frozenset({
    "frontend-tools", "decklink-output-ui", "decklink-captions",
    "aja-output-ui", "obs-websocket",
})


def _module_layouts(lib_dir: Path) -> list[tuple[str, Path, str, bool]]:
    """Places plugins may live, most specific first.

    Each entry is ``(kind, base, data_template, bundled)``: ``kind`` says how
    modules sit under ``base`` (see _layout_modules), ``data_template`` holds
    ``%module%``. OBS 33 moved plugins from ``obs-plugins/`` to
    ``obs-modules/core/`` (Linux), ``core/<module>/`` (Windows) and keeps them
    in ``PlugIns/<module>.plugin`` on macOS; the 32.x layouts stay as fallbacks.
    """
    system = platform.system()
    layouts: list[tuple[str, Path, str, bool]] = []
    if system == "Windows":
        layouts += [
            ("per_dir", lib_dir / "core", str(lib_dir / "core" / "%module%" / "data"), True),
            ("flat", lib_dir / "obs-plugins", str(lib_dir / "data" / "obs-plugins" / "%module%"), True),
        ]
        root = lib_dir.parent.parent            # <install>/bin/64bit -> <install>
        layouts += [
            ("per_dir", root / "core", str(root / "core" / "%module%" / "data"), False),
            ("flat", root / "obs-plugins" / "64bit", str(root / "data" / "obs-plugins" / "%module%"), False),
        ]
    elif system == "Darwin":
        # fetch_libs.py keeps libobs inside libobs.framework and flattens the
        # plugins to <root>/obs-plugins/<module>.dylib beside data/obs-plugins/.
        root = _bundled_root(lib_dir)
        if root is not None:
            layouts += [
                ("flat", root / "obs-plugins", str(root / "data" / "obs-plugins" / "%module%"), True),
            ]
        layouts += [
            ("macos", lib_dir.parent / "PlugIns" if lib_dir.name == "Frameworks" else lib_dir / "PlugIns", "", True),
            ("flat", lib_dir / "obs-plugins", str(lib_dir / "data" / "obs-plugins" / "%module%"), True),
            ("macos", Path("/Applications/OBS.app/Contents/PlugIns"), "", False),
        ]
    else:
        layouts += [
            ("flat", lib_dir / "obs-modules" / "core", str(lib_dir / "data" / "obs-modules" / "core" / "%module%"), True),
            ("flat", lib_dir / "obs-plugins", str(lib_dir / "data" / "obs-plugins" / "%module%"), True),
        ]
        import sysconfig
        multiarch = sysconfig.get_config_var("MULTIARCH") or "x86_64-linux-gnu"
        for prefix in ("/usr/local", "/usr"):
            for libsub in (f"lib/{multiarch}", "lib"):
                layouts.append(("flat", Path(prefix) / libsub / "obs-modules" / "core",
                                f"{prefix}/share/obs/obs-modules/core/%module%", False))
        for prefix in ("/usr/local", "/usr"):
            for libsub in (f"lib/{multiarch}", "lib"):
                layouts.append(("flat", Path(prefix) / libsub / "obs-plugins",
                                f"{prefix}/share/obs/obs-plugins/%module%", False))
    return layouts


def _layout_modules(kind: str, base: Path, data_template: str) -> list[tuple[str, str, str]]:
    """``(name, binary, data_dir)`` for each module found in one layout."""
    if not base.is_dir():
        return []
    suffix = _lib_suffix()
    found: list[tuple[str, str, str]] = []
    if kind == "flat":
        for binary in sorted(base.glob(f"*{suffix}")):
            name = binary.name[: -len(suffix)]
            # Linux packages put CEF/GL helpers (libcef.so, libEGL.so, …) next
            # to the modules; those are libraries, not OBS modules.
            if name.startswith("lib"):
                continue
            found.append((name, str(binary), data_template.replace("%module%", name)))
    elif kind == "per_dir":
        for mod_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            binary = mod_dir / f"{mod_dir.name}{suffix}"
            if binary.is_file():
                found.append((mod_dir.name, str(binary), data_template.replace("%module%", mod_dir.name)))
    elif kind == "macos":
        for bundle in sorted(base.glob("*.plugin")):
            name = bundle.name[: -len(".plugin")]
            binary = bundle / "Contents" / "MacOS" / name
            if binary.is_file():
                found.append((name, str(binary), str(bundle / "Contents" / "Resources")))
    return found


def get_obs_module_dirs() -> tuple[str, str]:
    """
    Return ``(plugin_bin_dir, plugin_data_template)`` of the first plugin layout
    that exists — the bundled one when present, else a system OBS install.

    Kept for callers of the 32.x API. Since OBS 33 a layout is not always one
    flat directory (Windows has ``core/<module>/``, macOS ``.plugin`` bundles),
    so prefer :func:`discover_modules`, which lists the modules themselves.
    """
    lib_dir = Path(find_libobs()).resolve().parent
    for _kind, base, data_template, _bundled in _module_layouts(lib_dir):
        if base.is_dir():
            return str(base), data_template
    return "", ""


def _lib_suffix() -> str:
    return {"Windows": ".dll", "Darwin": ".dylib"}.get(platform.system(), ".so")


def discover_modules(*, include_system: bool = True) -> list[tuple[str, str, str]]:
    """List the plugin modules to load, as ``(name, bin_path, data_path)``.

    Uses the first layout that contains any module: the plugins bundled inside
    the wheel when present, otherwise (if ``include_system``) a system OBS
    install. Qt frontend plugins are left out of system installs: they abort the
    process without a QApplication, which pylibobs never creates.
    """
    lib_dir = Path(find_libobs()).resolve().parent
    libs_root = _LIBS_DIR.resolve()
    for kind, base, data_template, bundled in _module_layouts(lib_dir):
        if not bundled and not include_system:
            continue
        # Only treat a layout as bundled when it really sits inside our package.
        if bundled and libs_root not in base.resolve().parents:
            continue
        modules = _layout_modules(kind, base, data_template)
        if not bundled:
            modules = [m for m in modules if m[0] not in _QT_FRONTEND_MODULES]
        if modules:
            return modules
    return []


def get_bundled_modules() -> list[tuple[str, str, str]]:
    """Enumerate the plugin modules bundled inside the wheel.

    Returns ``(name, bin_path, data_path)`` per module, or an empty list when
    nothing is bundled (running against a system OBS install).
    """
    return discover_modules(include_system=False)
