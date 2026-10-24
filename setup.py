"""Thin setup.py shim.

All metadata lives in pyproject.toml. This file exists for two narrow
reasons that need imperative Python code:

1. **Platform-specific wheel tag.** pylibobs bundles the libobs shared
   library (`.dll` / `.so` / `.dylib`) plus its plugin DLLs and data
   files, so a single universal `py3-none-any` wheel is wrong — the
   binaries differ per OS / architecture. Overriding
   `Distribution.has_ext_modules()` to return True makes setuptools
   tag the wheel with the host's platform string (e.g. `win_amd64`,
   `manylinux_2_31_x86_64`, `macosx_11_0_arm64`).

2. **Python-version-agnostic tag.** The Python code itself is pure
   Python (no compiled C extension specific to one CPython ABI), so
   we want the wheel tagged `py3-none-<platform>` instead of the
   default `cpXY-cpXY-<platform>`. Overriding `bdist_wheel.get_tag()`
   does this.

Together these produce wheels like:
    pylibobs-0.1.0-py3-none-win_amd64.whl
    pylibobs-0.1.0-py3-none-manylinux_2_31_x86_64.whl
    pylibobs-0.1.0-py3-none-macosx_11_0_arm64.whl
"""
from setuptools import setup
from setuptools.dist import Distribution
from setuptools.command.build_py import build_py as _BuildPy
import sys
import subprocess
from pathlib import Path

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


class BuildPy(_BuildPy):
    _native_library = Path("native/macos_surfaces.dylib")

    def _native_paths(self):
        package = Path(self.get_package_dir("pylibobs"))
        return package / self._native_library, Path(self.build_lib) / "pylibobs" / self._native_library

    def get_outputs(self, include_bytecode=True):
        outputs = super().get_outputs(include_bytecode)
        if sys.platform == "darwin":
            _, destination = self._native_paths()
            if str(destination) not in outputs:
                outputs.append(str(destination))
        return outputs

    def get_output_mapping(self):
        mapping = super().get_output_mapping()
        if sys.platform == "darwin" and self.editable_mode:
            source, destination = self._native_paths()
            mapping[str(destination)] = str(source)
        return mapping

    def run(self):
        super().run()
        if sys.platform == "darwin":
            source, destination = self._native_paths()
            # Editable imports resolve to the source package, including helpers.
            if self.editable_mode:
                destination = source
            destination.parent.mkdir(parents=True, exist_ok=True)
            subprocess.check_call([
                "clang", "-dynamiclib", "-O2", "-Wall", "-Wextra", "-Werror",
                "-Wno-deprecated-declarations", "-mmacosx-version-min=13.0",
                "-framework", "Cocoa", "-framework", "IOSurface", "-framework", "OpenGL",
                str(source.with_suffix(".m")), "-o", str(destination),
            ])
            subprocess.check_call(["codesign", "--force", "--sign", "-", str(destination)])


class BdistWheel(_BdistWheel):
    """Re-tag as `py3-none-<plat>` (pure-Python code + platform-specific data)."""
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
        return "py3", "none", plat


setup(
    distclass=BinaryDistribution,
    cmdclass={"bdist_wheel": BdistWheel, "build_py": BuildPy},
)
