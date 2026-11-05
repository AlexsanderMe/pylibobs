"""Build and publish upstream-pinned wheel snapshots in a fork (Python 3.11+)."""

from __future__ import annotations

import argparse
import ast
from email.parser import BytesParser
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile


UPSTREAM = "jonata/pylibobs"
SHA_RE = re.compile(r"[0-9a-f]{40}")
TARGETS = ("windows-x86_64", "linux-x86_64", "macos-arm64", "macos-x86_64")
REQUIRED = {
    "windows": (
        "obs.dll", "libobs-d3d11.dll", "obs-ffmpeg-mux.exe", "data/libobs/*",
        "core/obs-ffmpeg/obs-ffmpeg.dll", "core/image-source/image-source.dll",
        "core/obs-transitions/obs-transitions.dll", "core/win-dshow/win-dshow.dll",
        "core/win-dshow/data/obs-virtualcam-module64.dll",
        "core/win-dshow/data/obs-virtualcam-module32.dll",
    ),
    "linux": (
        "libobs.so*", "libobs-opengl.so*", "data/libobs/*",
        "obs-modules/core/obs-ffmpeg.so", "obs-modules/core/image-source.so",
        "obs-modules/core/obs-transitions.so", "obs-modules/core/linux-v4l2.so",
    ),
    "macos": (
        "Frameworks/libobs.framework/Versions/A/libobs", "Frameworks/libobs-opengl.dylib",
        "Frameworks/libobs.framework/Versions/A/Resources/*.effect", "runtime.json",
        "obs-plugins/mac-avcapture.dylib", "obs-plugins/image-source.dylib",
        "obs-plugins/obs-transitions.dylib", "obs-plugins/obs-ffmpeg.dylib", "obs-ffmpeg-mux",
    ),
}


def request_json(url: str, *, missing_ok: bool = False):
    headers = {"Accept": "application/json", "User-Agent": "pylibobs-snapshots"}
    if url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"
    try:
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        raise


def api(path: str, *, missing_ok: bool = False):
    return request_json(f"https://api.github.com/repos/{path}", missing_ok=missing_ok)


def require_sha(value: str) -> str:
    if not SHA_RE.fullmatch(value):
        raise ValueError("Use a full, lowercase 40-character Git commit SHA")
    return value


def require_ancestor(base: str, head: str) -> None:
    comparison = api(f"{UPSTREAM}/compare/{base}...{head}")
    if comparison["status"] not in ("ahead", "identical"):
        raise ValueError(f"{base} is not an ancestor of {head} in {UPSTREAM}")


def constant(path: Path, name: str) -> str:
    for statement in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str):
                return value
    raise ValueError(f"Missing string constant {name} in {path}")


def source_versions(source: Path) -> tuple[str, str]:
    version = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    obs = constant(source / "scripts/fetch_libs.py", "DEFAULT_OBS_TAG")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.[1-9]\d*)?", version):
        raise ValueError("Snapshot source must have a public pylibobs release version")
    if version != constant(source / "pylibobs/__init__.py", "__version__"):
        raise ValueError("Upstream package versions disagree")
    if ".".join(version.split(".")[:3]) != obs:
        raise ValueError("Upstream package version and bundled OBS pin disagree")
    return version, obs


def comparison_base(repository: str) -> tuple[str, str]:
    page = 1
    snapshots = []
    while True:
        releases = api(f"{repository}/releases?per_page=100&page={page}")
        for release in releases:
            if release["draft"] or not release["tag_name"].startswith("snapshot-"):
                continue
            snapshots.append(release)
        if len(releases) < 100:
            break
        page += 1
    if snapshots:
        # Release-list order follows commit creation, not publication. Snapshot
        # tags can share one recipe commit, so choose the actual latest publish.
        release = max(snapshots, key=lambda item: item["published_at"])
        manifests = [asset for asset in release["assets"] if asset["name"] == "provenance.json"]
        if len(manifests) != 1:
            raise ValueError(f"Cannot establish the source of {release['tag_name']}")
        provenance = request_json(manifests[0]["browser_download_url"])
        if (provenance.get("schema") != 1 or provenance.get("upstream_repository") != UPSTREAM
                or provenance.get("tag") != release["tag_name"]):
            raise ValueError(f"Invalid provenance for {release['tag_name']}")
        return require_sha(provenance["source_sha"]), release["tag_name"]
    published = request_json("https://pypi.org/pypi/pylibobs/json")["info"]["version"]
    tag = f"v{published}"
    commit = api(f"{UPSTREAM}/commits/{tag}")["sha"]
    return require_sha(commit), tag


def ensure_unused(repository: str, tag: str) -> None:
    for endpoint in (f"releases/tags/{tag}", f"git/ref/tags/{tag}"):
        if api(f"{repository}/{endpoint}", missing_ok=True) is not None:
            raise ValueError(f"{tag} already exists; use a new build revision")


def plan_snapshot(source: Path, sha: str, revision: str, repository: str, recipe_sha: str) -> dict:
    require_sha(sha)
    require_sha(recipe_sha)
    if not re.fullmatch(r"[1-9]\d{0,8}", revision):
        raise ValueError("Build revision must be a positive integer of at most nine digits")
    checked_out = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
    ).strip()
    if checked_out != sha:
        raise ValueError("Source checkout does not match the requested upstream commit")
    version, obs = source_versions(source)
    short = sha[:12]
    tag = f"snapshot-{version}-g{short}-r{revision}"
    ensure_unused(repository, tag)
    base, label = comparison_base(repository)
    require_ancestor(base, sha)
    return {
        "schema": 1, "repository": repository, "upstream_repository": UPSTREAM,
        "source_sha": sha, "recipe_sha": recipe_sha, "upstream_version": version,
        "obs_version": obs, "build_revision": int(revision),
        "version": f"{version}+g{short}.r{revision}", "tag": tag,
        "comparison_sha": base, "comparison_label": label,
        "run_url": f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
    }


def stamp_version(source: Path, plan: dict) -> None:
    version, _ = source_versions(source)
    if version != plan["upstream_version"]:
        raise ValueError("Checkout version does not match the snapshot plan")
    replacements = []
    for relative, pattern in (
        ("pyproject.toml", r'(?m)^(version\s*=\s*")[^"]+("[ \t]*)$'),
        ("pylibobs/__init__.py", r'(?m)^(__version__\s*=\s*")[^"]+("[ \t]*)$'),
    ):
        path = source / relative
        text, count = re.subn(pattern, lambda match: match[1] + plan["version"] + match[2],
                              path.read_text(encoding="utf-8"))
        if count != 1:
            raise ValueError(f"Expected exactly one version assignment in {relative}")
        replacements.append((path, text))
    for path, text in replacements:
        path.write_text(text, encoding="utf-8")


def wheel_target(path: Path) -> str:
    platform = path.stem.rsplit("-", 1)[-1]
    if platform == "win_amd64":
        return "windows-x86_64"
    if re.fullmatch(r"manylinux_2_\d+_x86_64", platform):
        return "linux-x86_64"
    match = re.fullmatch(r"macosx_\d+_\d+_(arm64|x86_64)", platform)
    if match:
        return f"macos-{match[1]}"
    raise ValueError(f"Unexpected wheel platform: {path.name}")


def inspect_wheel(path: Path, plan: dict, target: str) -> None:
    if wheel_target(path) != target:
        raise ValueError(f"Wheel does not target {target}: {path.name}")
    platform, arch = target.split("-", 1)
    with zipfile.ZipFile(path) as archive:
        files = archive.namelist()
        metadata_paths = [name for name in files if name.endswith(".dist-info/METADATA")]
        if len(metadata_paths) != 1:
            raise ValueError("Expected one wheel metadata record")
        metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
        if metadata["Name"] != "pylibobs" or metadata["Version"] != plan["version"]:
            raise ValueError("Wheel metadata does not match the snapshot plan")
        if not path.name.startswith(f"pylibobs-{plan['version']}-py3-none-"):
            raise ValueError("Wheel filename must identify the same Python-independent snapshot")
        patterns = [f"pylibobs/_libs/{platform}/{arch}/{name}" for name in REQUIRED[platform]]
        if platform == "macos":
            patterns += [f"pylibobs/native/{name}.dylib" for name in ("macos_surfaces", "macos_camera")]
        missing = [pattern for pattern in patterns if not any(
            fnmatch.fnmatchcase(name, pattern) and archive.getinfo(name).file_size > 0 for name in files
        )]
        if missing:
            raise ValueError(f"Wheel lacks required runtime files: {missing}")


def retag_linux(source: Path) -> None:
    minimum = 0
    for path in (source / "pylibobs/_libs/linux").rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as stream:
            if stream.read(4) == b"\x7fELF":
                minimum = max([minimum] + [int(value) for value in re.findall(
                    rb"GLIBC_2\.(\d+)", stream.read())])
    if minimum == 0:
        raise ValueError("No GLIBC requirements found in the bundled ELF files")
    wheels = list((source / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("Expected one Linux wheel")
    subprocess.run([sys.executable, "-m", "wheel", "tags", "--remove",
                    "--platform-tag", f"manylinux_2_{minimum}_x86_64", str(wheels[0])], check=True)


def smoke_wheel(path: Path, plan: dict) -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps",
                    str(path.resolve())], check=True)
    script = """
import ctypes, importlib.metadata, json, sys
from pathlib import Path
import pylibobs
from pylibobs._ffi import get_lib, ffi
from pylibobs._lib import find_libobs
plan = json.loads(sys.argv[1])
assert importlib.metadata.version('pylibobs') == pylibobs.__version__ == plan['version']
path = Path(find_libobs()).resolve()
assert (Path(pylibobs.__file__).resolve().parent / '_libs') in path.parents, path
assert ffi.string(get_lib().obs_get_version_string()).decode() == plan['obs_version']
if sys.platform == 'darwin':
    for name in ('macos_surfaces', 'macos_camera'):
        ctypes.CDLL(str(Path(pylibobs.__file__).parent / 'native' / (name + '.dylib')))
print('Installed wheel metadata, bundled libobs, and native helpers verified')
"""
    with tempfile.TemporaryDirectory() as temporary:
        subprocess.run([sys.executable, "-I", "-c", script, json.dumps(plan)],
                       cwd=temporary, check=True)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def assemble(dist: Path, plan: dict) -> None:
    wheels = sorted(dist.glob("*.whl"))
    targets = [wheel_target(path) for path in wheels]
    if sorted(targets) != sorted(TARGETS):
        raise ValueError(f"Expected exactly one wheel for each supported target; got {targets}")
    for path, target in zip(wheels, targets):
        inspect_wheel(path, plan, target)
    checksums = {path.name: digest(path) for path in wheels}
    provenance = dict(plan, artifacts=checksums)
    (dist / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    checksums["provenance.json"] = digest(dist / "provenance.json")
    (dist / "SHA256SUMS").write_text(
        "".join(f"{checksum}  {name}\n" for name, checksum in checksums.items()), encoding="utf-8")
    root = f"https://github.com/{UPSTREAM}"
    notes = (
        f"Unofficial snapshot of [pylibobs@{plan['source_sha'][:12]}]"
        f"({root}/commit/{plan['source_sha']}), bundling OBS {plan['obs_version']}.\n\n"
        f"[Changes since {plan['comparison_label']}]"
        f"({root}/compare/{plan['comparison_sha']}...{plan['source_sha']}) · "
        f"[Build and validation]({plan['run_url']})\n\n"
        "Wheels for Windows x64, Linux x64, and macOS Intel/Apple Silicon.\n"
        "Checksums: `SHA256SUMS`. Source and build revisions: `provenance.json`.\n"
    )
    (dist.parent / "release-notes.md").write_text(notes, encoding="utf-8")


def publish(dist: Path, plan: dict) -> None:
    if (os.environ["GITHUB_REPOSITORY"] != plan["repository"]
            or os.environ["GITHUB_SHA"] != plan["recipe_sha"]):
        raise ValueError("Publication repository or recipe commit does not match the plan")
    ensure_unused(plan["repository"], plan["tag"])
    title = f"pylibobs {plan['upstream_version']} — snapshot {plan['source_sha'][:12]} · build {plan['build_revision']}"
    subprocess.run([
        "gh", "release", "create", plan["tag"], *[str(path) for path in sorted(dist.iterdir())],
        "--repo", plan["repository"], "--target", plan["recipe_sha"], "--title", title,
        "--notes-file", str(dist.parent / "release-notes.md"), "--draft", "--prerelease", "--latest=false",
    ], check=True)
    subprocess.run(["gh", "release", "edit", plan["tag"], "--repo", plan["repository"],
                    "--draft=false", "--prerelease", "--latest=false"], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate-source", "plan", "stamp", "retag-linux",
                                            "verify-wheel", "assemble", "publish"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--platform", choices=("windows", "linux", "macos"))
    parser.add_argument("--arch", choices=("x86_64", "arm64"))
    args = parser.parse_args()
    if args.command == "validate-source":
        sha = require_sha(os.environ["UPSTREAM_SHA"])
        require_ancestor(sha, "main")
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
            stream.write(f"source_sha={sha}\n")
    elif args.command == "plan":
        plan = plan_snapshot(args.source, os.environ["UPSTREAM_SHA"], os.environ["BUILD_REVISION"],
                             os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"])
        args.plan.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    elif args.command == "retag-linux":
        retag_linux(args.source)
    else:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        if args.command == "stamp":
            stamp_version(args.source, plan)
        elif args.command == "verify-wheel":
            wheels = list((args.source / "dist").glob("*.whl"))
            if len(wheels) != 1:
                raise ValueError("Expected exactly one built wheel")
            inspect_wheel(wheels[0], plan, f"{args.platform}-{args.arch}")
            smoke_wheel(wheels[0], plan)
        elif args.command == "assemble":
            assemble(args.dist, plan)
        elif args.command == "publish":
            publish(args.dist, plan)


if __name__ == "__main__":
    main()
