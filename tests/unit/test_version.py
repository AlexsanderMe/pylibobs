"""pylibobs X.Y.Z bundles libobs X.Y.Z (X.Y.Z.N for pylibobs-only releases)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
try:
    import check_version  # noqa: E402
finally:
    sys.path.pop(0)


def test_repository_versions_agree():
    assert check_version.problems(*check_version.read_versions(), tag=None) == []


@pytest.mark.parametrize("version", ["32.2.2", "32.2.2.1", "32.2.2.12"])
def test_scheme_accepts(version):
    assert check_version.problems(version, version, "32.2.2", tag=f"v{version}") == []


@pytest.mark.parametrize("pyproject, init, pin, tag, expected", [
    ("32.2.3", "32.2.3", "32.2.2", None, "bundles 32.2.2"),         # pin not bumped
    ("32.2.2", "32.2.1", "32.2.2", None, "pylibobs.__version__"),  # the two strings drifted
    ("0.1.3", "0.1.3", "32.2.2", None, "claims libobs 0.1.3"),      # old scheme
    ("32.2.2-1", "32.2.2-1", "32.2.2", None, "is not"),            # not <libobs>.<n>
    ("32.2.2.0", "32.2.2.0", "32.2.2", None, "is not"),            # .0 means nothing
    ("32.2.2.01", "32.2.2.01", "32.2.2", None, "is not"),          # PEP 440 would rename it
    ("32.2.2", "32.2.2", "32.2.2", "v32.2.2.1", "does not match"),  # wrong tag
])
def test_scheme_rejects(pyproject, init, pin, tag, expected):
    found = check_version.problems(pyproject, init, pin, tag)
    assert any(expected in p for p in found), found
