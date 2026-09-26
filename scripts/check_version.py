#!/usr/bin/env python3
"""
Check that the release version follows the libobs version it bundles.

pylibobs X.Y.Z ships libobs X.Y.Z; releases that only change pylibobs on the
same libobs add a fourth number: X.Y.Z.1, X.Y.Z.2, ... So:

  * pyproject.toml's version and pylibobs.__version__ must be identical,
  * its first three numbers must equal DEFAULT_OBS_TAG in fetch_libs.py,
  * and on a tag push, the tag must be "v" + that version.

Run by the release workflow before anything is built, since a PyPI upload
cannot be replaced; also safe to run locally. Needs nothing beyond the stdlib.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# X.Y.Z, or X.Y.Z.N with N >= 1; no leading zeros (PEP 440 would normalize them away)
_NUM = r"(?:0|[1-9]\d*)"
VERSION_RE = re.compile(rf"^({_NUM}\.{_NUM}\.{_NUM})(?:\.([1-9]\d*))?$")


def read_versions() -> tuple[str, str, str]:
    # A regex rather than tomllib, which only exists from Python 3.11 while
    # pylibobs (and its unit tests) support 3.9+. Scoped to the [project] table.
    text = (ROOT / "pyproject.toml").read_text()
    project = re.search(r"^\[project\]$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    pyproject = project and re.search(r'^version\s*=\s*"([^"]+)"', project.group(1), re.M)
    init = re.search(r'^__version__ = "([^"]+)"', (ROOT / "pylibobs" / "__init__.py").read_text(), re.M)
    pin = re.search(r'^DEFAULT_OBS_TAG = "([^"]+)"', (ROOT / "scripts" / "fetch_libs.py").read_text(), re.M)
    if not pyproject or not init or not pin:
        sys.exit("Could not find [project] version in pyproject.toml, __version__ in "
                 "pylibobs/__init__.py or DEFAULT_OBS_TAG in scripts/fetch_libs.py")
    return pyproject.group(1), init.group(1), pin.group(1)


def problems(pyproject: str, init: str, pin: str, tag: str | None) -> list[str]:
    out = []
    if pyproject != init:
        out.append(f"pyproject.toml says {pyproject}, pylibobs.__version__ says {init}")
    m = VERSION_RE.match(pyproject)
    if not m:
        out.append(f"{pyproject} is not <libobs version> or <libobs version>.<n>")
    elif m.group(1) != pin:
        out.append(f"{pyproject} claims libobs {m.group(1)}, but fetch_libs.py bundles {pin}")
    if tag is not None and tag != f"v{pyproject}":
        out.append(f"tag {tag} does not match version {pyproject} (expected v{pyproject})")
    return out


def main() -> None:
    pyproject, init, pin = read_versions()
    tag = os.environ.get("REF_NAME") if os.environ.get("REF_TYPE") == "tag" else None
    found = problems(pyproject, init, pin, tag)
    if found:
        sys.exit("Version check failed:\n  " + "\n  ".join(found))
    print(f"OK: pylibobs {pyproject} bundles libobs {pin}" + (f", tag {tag}" if tag else ""))


if __name__ == "__main__":
    main()
