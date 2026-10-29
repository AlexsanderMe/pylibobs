"""Packaging contracts for official asset integrity and the macOS layout."""
from __future__ import annotations

import hashlib
import plistlib
import sys
from unittest.mock import Mock

import pytest

from scripts import fetch_libs


def test_download_revalidates_cached_asset(monkeypatch, tmp_path):
    destination = tmp_path / "OBS.dmg"
    destination.write_bytes(b"damaged cache")
    payload = b"official release bytes"
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = iter([payload])
    monkeypatch.setattr(fetch_libs.requests, "get", Mock(return_value=response))
    fetch_libs.download("https://example.test/OBS.dmg", destination, hashlib.sha256(payload).hexdigest())
    assert destination.read_bytes() == payload
    fetch_libs.requests.get.reset_mock()
    fetch_libs.download("https://example.test/OBS.dmg", destination, hashlib.sha256(payload).hexdigest())
    fetch_libs.requests.get.assert_not_called()


def test_download_rejects_corrupt_asset_atomically(monkeypatch, tmp_path):
    destination = tmp_path / "OBS.dmg"
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = iter([b"incomplete or corrupt"])
    monkeypatch.setattr(fetch_libs.requests, "get", Mock(return_value=response))
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        fetch_libs.download("https://example.test/OBS.dmg", destination, "0" * 64)
    assert not destination.exists()
    assert not list(tmp_path.glob("*.part"))


# The fake OBS.app needs framework symlinks, which Windows runners can't
# create; the extraction itself only ever runs on macOS.
@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
def test_macos_extraction_preserves_bundle_and_headless_module_resources(tmp_path):
    contents = tmp_path / "OBS.app/Contents"
    files = {
        "Frameworks/libobs.framework/Versions/A/libobs": b"native executable",
        "Frameworks/libobs.framework/Versions/A/Resources/default.effect": b"shader",
        "Frameworks/libobs-opengl.dylib": b"graphics backend",
        "MacOS/obs-ffmpeg-mux": b"mux executable",
        "PlugIns/mac-avcapture.plugin/Contents/MacOS/mac-avcapture": b"capture plugin",
        "PlugIns/mac-avcapture.plugin/Contents/Resources/locale/en-US.ini": b"Capture=Camera",
        "PlugIns/aja-output-ui.plugin/Contents/MacOS/aja-output-ui": b"Qt-only module",
        "PlugIns/mac-virtualcam.plugin/Contents/MacOS/mac-virtualcam": b"frontend-dependent module",
    }
    for name, data in files.items():
        path = contents / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (contents / "Info.plist").write_bytes(plistlib.dumps({"LSMinimumSystemVersion": "13.0"}))
    framework = contents / "Frameworks/libobs.framework"
    (framework / "Versions/Current").symlink_to("A", target_is_directory=True)
    (framework / "libobs").symlink_to("Versions/Current/libobs")
    destination = tmp_path / "runtime"
    fetch_libs._copy_macos_runtime(contents, destination)
    assert (destination / "Frameworks/libobs.framework/libobs").read_bytes() == b"native executable"
    assert (destination / "Frameworks/libobs.framework/libobs").is_symlink()
    assert not (destination / "Frameworks/libobs.dylib").exists()
    assert (destination / "data/libobs/default.effect").read_bytes() == b"shader"
    assert (destination / "obs-plugins/mac-avcapture.dylib").is_file()
    assert (destination / "data/obs-plugins/mac-avcapture/locale/en-US.ini").is_file()
    assert not (destination / "obs-plugins/aja-output-ui.dylib").exists()
    assert not (destination / "obs-plugins/mac-virtualcam.dylib").exists()


@pytest.mark.parametrize("existing_rpath", ["@executable_path/../Frameworks", "@loader_path/../../..", None])
def test_macos_relocation_reuses_host_rpath_without_growing_load_commands(
    monkeypatch, tmp_path, existing_rpath,
):
    binary = tmp_path / "Frameworks/libobs.framework/Versions/A/libobs"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    desired = "@loader_path/../../.."
    calls = []

    def inspect(command, **_kwargs):
        if command[0] == "lipo":
            return "arm64\n"
        if "-L" in command:
            return f"{binary}:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
        return f"    cmd LC_RPATH\n   path {existing_rpath} (offset 12)\n" if existing_rpath else ""

    monkeypatch.setattr(fetch_libs.subprocess, "check_output", inspect)
    monkeypatch.setattr(fetch_libs.subprocess, "check_call", lambda command: calls.append(command))
    fetch_libs._relocate_macos_runtime(tmp_path, "arm64")
    edits = [command for command in calls if command[0] == "install_name_tool"]
    if existing_rpath == desired:
        assert not edits
    elif existing_rpath is not None:
        assert edits == [["install_name_tool", "-rpath", existing_rpath, desired, str(binary)]]
    else:
        assert edits == [["install_name_tool", "-add_rpath", desired, str(binary)]]
    assert calls[-1][0] == "codesign"


@pytest.mark.parametrize("platform", ["macos", "windows", "linux"])
def test_cli_uses_explicit_shared_cache_without_filesystem_links(monkeypatch, tmp_path, platform):
    import sys
    calls = []
    cache = tmp_path / "shared/cache"
    monkeypatch.setattr(sys, "argv", ["fetch_libs.py", "--platform", platform, "--arch", "x86_64",
                                      "--cache-dir", str(cache)])
    monkeypatch.setattr(fetch_libs, "get_release", lambda _version: {"tag_name": "32.2.2"})
    monkeypatch.setattr(fetch_libs, "fetch_for", lambda *args: calls.append(args) or True)
    assert fetch_libs.main() == 0
    assert calls[0][-1] == cache.resolve()
    assert cache.is_dir() and not cache.is_symlink()
