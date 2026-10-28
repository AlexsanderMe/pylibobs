"""Thin setup.py shim.

All metadata lives in pyproject.toml. This file exists for two narrow
reasons that need imperative Python code:

1. **Platform-specific wheel tag.** pylibobs bundles the libobs shared
   library (`.dll` / `.so` / `.dylib`) plus its plugin DLLs and data
   files, so a single universal `py3-none-any` wheel is wrong — the
   binaries differ per OS / architecture. Overriding
   `Distribution.has_ext_modules()` to return True makes setuptools
   tag the wheel with the host's platform string (e.g. `win_amd64`,
   `linux_x86_64`, retagged `manylinux_2_43_x86_64` by CI, `macosx_11_0_arm64`).

2. **Python-version-agnostic tag.** The Python code itself is pure
   Python (no compiled C extension specific to one CPython ABI), so
   we want the wheel tagged `py3-none-<platform>` instead of the
   default `cpXY-cpXY-<platform>`. Overriding `bdist_wheel.get_tag()`
   does this.

Together these produce wheels like:
    pylibobs-33.0.0-py3-none-win_amd64.whl
    pylibobs-33.0.0-py3-none-manylinux_2_43_x86_64.whl
    pylibobs-33.0.0-py3-none-macosx_11_0_arm64.whl
"""
import os
from pathlib import Path

from setuptools import setup
from setuptools.dist import Distribution

try:                              # setuptools >= 70
    from setuptools.command.bdist_wheel import bdist_wheel as _BdistWheel
except ImportError:               # older setuptools
    from wheel.bdist_wheel import bdist_wheel as _BdistWheel  # type: ignore


class BinaryDistribution(Distribution):
    """Force a platform-specific wheel because we bundle libobs binaries."""
    def has_ext_modules(self):
        return True
    def is_pure(self):
        return False


#: Set to 1 to build a wheel with no bundled libobs, for use with a system OBS.
ALLOW_SYSTEM_LIBOBS = "PYLIBOBS_ALLOW_SYSTEM_LIBOBS"
_LIBOBS_NAMES = ("libobs.so*", "obs.dll", "libobs.dylib", "Frameworks/libobs.dylib")


def _has_bundled_libobs() -> bool:
    libs = Path(__file__).resolve().parent / "pylibobs" / "_libs"
    return any(next(libs.glob(f"*/*/{name}"), None) is not None for name in _LIBOBS_NAMES)


class BdistWheel(_BdistWheel):
    """Re-tag as `py3-none-<plat>` (pure-Python code + platform-specific data)."""
    def run(self):
        # pip builds a wheel like this when it installs the sdist, which it
        # does whenever no published wheel fits the system — e.g. Linux with
        # glibc < 2.43 for pylibobs 33. The sdist carries no libobs, and a
        # pylibobs without one would silently bind whatever libobs the system
        # has, of whatever version. Fail with a pointer instead.
        if not _has_bundled_libobs() and os.environ.get(ALLOW_SYSTEM_LIBOBS) != "1":
            raise SystemExit(
                "pylibobs: no bundled libobs to put in this wheel.\n"
                "pip is building pylibobs from source, which happens when no published\n"
                "wheel fits this system. pylibobs 33's Linux wheel needs glibc 2.43+\n"
                "(Ubuntu 26.04 or newer); on older systems install the OBS 32 release:\n"
                "    pip install \"pylibobs<33\"\n"
                "To build against a system-installed OBS 33 instead, set\n"
                f"{ALLOW_SYSTEM_LIBOBS}=1. Maintainers: run scripts/fetch_libs.py first."
            )
        super().run()

    def finalize_options(self):
        super().finalize_options()
        self.root_is_pure = False
    def get_tag(self):
        _py, _abi, plat = super().get_tag()
        # On Apple Silicon CI runners, setuptools picks up the host
        # Python's "universal2" tag — but our bundled libobs is single-
        # architecture (we extract only the matching slice from OBS.app).
        # Force the tag to the real architecture so PyPI accepts the
        # wheel and pip selects it correctly.
        if "universal2" in plat:
            import platform as _platform
            host_arch = _platform.machine().lower()
            if host_arch in ("arm64", "aarch64"):
                plat = plat.replace("universal2", "arm64")
            elif host_arch in ("x86_64", "amd64"):
                plat = plat.replace("universal2", "x86_64")
        # The bundled OBS runtime can require a newer OS than the Python
        # interpreter used to build the wheel. Never advertise Python's lower
        # deployment target for those native libraries.
        if plat.startswith("macosx_"):
            import json
            import platform as _platform
            from pathlib import Path
            arch = {"aarch64": "arm64", "amd64": "x86_64"}.get(
                _platform.machine().lower(), _platform.machine().lower())
            manifest = Path(__file__).parent / "pylibobs/_libs/macos" / arch / "runtime.json"
            if manifest.is_file():
                minimum = json.loads(manifest.read_text())["macos_deployment_target"]
                major, minor, *_ = [int(part) for part in minimum.split(".")]
                plat = f"macosx_{major}_{minor}_{arch}"
        return "py3", "none", plat


setup(
    distclass=BinaryDistribution,
    cmdclass={"bdist_wheel": BdistWheel},
)
