"""Native helpers remain discoverable in wheel and editable builds."""
from pathlib import Path
import runpy
import shutil
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from setuptools.dist import Distribution


@pytest.fixture
def native_build(monkeypatch, tmp_path):
    repository = Path(__file__).resolve().parents[2]
    setup = Mock()
    monkeypatch.setattr("setuptools.setup", setup)
    runpy.run_path(str(repository / "setup.py"))
    command_type = setup.call_args.kwargs["cmdclass"]["build_py"]
    package = tmp_path / "pylibobs"
    native = package / "native"
    native.mkdir(parents=True)
    (package / "__init__.py").touch()
    for source in (repository / "pylibobs/native").glob("*.m"):
        shutil.copy2(source, native / source.name)
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(repository / name, tmp_path / name)
    monkeypatch.chdir(tmp_path)
    distribution = Distribution()
    distribution.script_name = "setup.py"
    distribution.parse_config_files()
    command = command_type(distribution)
    command.ensure_finalized()
    command.build_lib = str(tmp_path / "build/lib")
    monkeypatch.setitem(command.run.__globals__, "sys", SimpleNamespace(platform="darwin"))
    calls = []

    def compile_helper(arguments):
        calls.append(arguments)
        if arguments[0] == "clang":
            assert Path(arguments[arguments.index("-o") - 1]).is_file()
            Path(arguments[arguments.index("-o") + 1]).write_bytes(b"native helper")

    monkeypatch.setattr("subprocess.check_call", compile_helper)
    return SimpleNamespace(command=command, package=package, calls=calls)


@pytest.mark.parametrize("editable", [False, True])
def test_native_helper_is_available_at_its_runtime_location(native_build, editable):
    command = native_build.command
    command.editable_mode = editable
    # Backends may inspect outputs before executing their build subcommands.
    command.get_outputs()
    command.run()
    runtime_package = native_build.package if editable else Path(command.build_lib) / "pylibobs"
    libraries = list((runtime_package / "native").glob("*.dylib"))
    assert libraries, "the loader cannot find a helper in the imported package"
    for library in libraries:
        build_output = Path(command.build_lib) / "pylibobs/native" / library.name
        assert str(build_output) in command.get_outputs()
        if editable:
            assert Path(command.get_output_mapping()[str(build_output)]).resolve() == library
    assert native_build.calls[-1][0] == "codesign"


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_other_platforms_do_not_build_cocoa_helpers(native_build, monkeypatch, platform):
    command = native_build.command
    monkeypatch.setitem(command.run.__globals__, "sys", SimpleNamespace(platform=platform))
    command.run()
    assert not native_build.calls
    assert not list(native_build.package.rglob("*.dylib"))
