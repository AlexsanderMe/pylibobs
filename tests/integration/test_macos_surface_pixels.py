"""Real IOSurface transport: verify pixels, alpha, scissor isolation, and resize."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration

if platform.system() != "Darwin":
    pytest.skip("requires macOS IOSurface/Cocoa", allow_module_level=True)

pytest.importorskip("AppKit", reason="PyObjC creates the test's native consumer window")


@pytest.mark.parametrize("case", ["graph", "translucent", "scissored"])
def test_pixels_survive_cross_process_delivery(case):
    from pylibobs import macos_surfaces

    # Use exactly the selected package in parent and spawned producer. Keeping
    # the whole smoke in a subprocess isolates Cocoa and libobs lifecycle state.
    package_root = Path(macos_surfaces.__file__).resolve().parent.parent
    example = Path(__file__).resolve().parents[2] / "examples/macos_surface_smoke.py"
    environment = {**os.environ, "PYTHONPATH": str(package_root)}
    result = subprocess.run(
        [sys.executable, str(example), "--seconds", "3", "--case", case],
        env=environment, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
