"""Snapshot release contracts, using synthetic sources and wheel archives only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
import zipfile

import pytest

pytest.importorskip("tomllib", reason="snapshot release tooling requires Python 3.11+")
from scripts import snapshot_release as release  # noqa: E402


SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
RECIPE_SHA = "f" * 40
BASE_SHA = "a" * 40
REPOSITORY = "example/pylibobs"
PUBLIC_VERSION = "33.0.0.2"
SNAPSHOT_VERSION = "33.0.0.2+g0123456789ab.r7"
SNAPSHOT_TAG = "snapshot-33.0.0.2-g0123456789ab-r7"
PREVIOUS_TAG = "snapshot-33.0.0-gprevious-r1"
MANIFEST_URL = f"https://github.com/{REPOSITORY}/releases/download/{PREVIOUS_TAG}/provenance.json"

# Independent examples of the shipped runtime layout, not generated from REQUIRED.
RUNTIME_FILES = {
    "windows": (
        "obs.dll", "libobs-d3d11.dll", "obs-ffmpeg-mux.exe", "data/libobs/default.effect",
        "core/obs-ffmpeg/obs-ffmpeg.dll", "core/image-source/image-source.dll",
        "core/obs-transitions/obs-transitions.dll", "core/win-dshow/win-dshow.dll",
        "core/win-dshow/data/obs-virtualcam-module64.dll",
        "core/win-dshow/data/obs-virtualcam-module32.dll",
    ),
    "linux": (
        "libobs.so.33", "libobs-opengl.so.33", "data/libobs/default.effect",
        "obs-modules/core/obs-ffmpeg.so", "obs-modules/core/image-source.so",
        "obs-modules/core/obs-transitions.so", "obs-modules/core/linux-v4l2.so",
    ),
    "macos": (
        "Frameworks/libobs.framework/Versions/A/libobs", "Frameworks/libobs-opengl.dylib",
        "Frameworks/libobs.framework/Versions/A/Resources/default.effect", "runtime.json",
        "obs-plugins/mac-avcapture.dylib", "obs-plugins/image-source.dylib",
        "obs-plugins/obs-transitions.dylib", "obs-plugins/obs-ffmpeg.dylib", "obs-ffmpeg-mux",
    ),
}
PLATFORM_TAGS = {
    "windows-x86_64": "win_amd64",
    "linux-x86_64": "manylinux_2_39_x86_64",
    "macos-arm64": "macosx_13_0_arm64",
    "macos-x86_64": "macosx_13_0_x86_64",
}


@pytest.fixture(autouse=True)
def isolated_external_effects(monkeypatch):
    monkeypatch.setattr(release, "urlopen", Mock(side_effect=AssertionError("Unexpected network access")))
    monkeypatch.setattr(release, "subprocess", SimpleNamespace(
        run=Mock(side_effect=AssertionError("Unexpected subprocess")),
        check_output=Mock(side_effect=AssertionError("Unexpected git invocation")),
    ))
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_SHA", RECIPE_SHA)
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")


@pytest.fixture
def mock_api(monkeypatch):
    def install(responses):
        def respond(endpoint, **kwargs):
            assert endpoint in responses, f"Unexpected API endpoint: {endpoint}"
            return responses[endpoint]
        mocked = Mock(side_effect=respond)
        monkeypatch.setattr(release, "api", mocked)
        return mocked
    return install


def previous_release(**overrides):
    return dict({
        "draft": False, "tag_name": PREVIOUS_TAG, "published_at": "2026-10-01T12:00:00Z",
        # Editable notes deliberately contradict the immutable manifest.
        "body": f"https://github.com/jonata/pylibobs/commit/{'b' * 40}",
        "assets": [{"name": "provenance.json", "browser_download_url": MANIFEST_URL}],
    }, **overrides)


@pytest.fixture
def previous_provenance(monkeypatch):
    manifest = {
        "schema": 1, "upstream_repository": "jonata/pylibobs",
        "tag": PREVIOUS_TAG, "source_sha": BASE_SHA,
    }
    def respond(url, **kwargs):
        assert url == MANIFEST_URL, f"Unexpected JSON URL: {url}"
        return manifest
    monkeypatch.setattr(release, "request_json", Mock(side_effect=respond))
    return manifest


@pytest.fixture
def source_factory(tmp_path):
    def make(version=PUBLIC_VERSION, package_version=None, obs="33.0.0"):
        root = tmp_path / "source"
        (root / "pylibobs").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "pylibobs"\nversion = "{version}"\n'
            f'description = "Keep {version} in this description"\n', encoding="utf-8")
        (root / "pylibobs/__init__.py").write_text(
            f'"""Keep {version} in this docstring."""\n'
            f'__version__ = "{package_version if package_version is not None else version}"\n'
            'raise RuntimeError("Source must be parsed, never imported")\n', encoding="utf-8")
        (root / "scripts/fetch_libs.py").write_text(
            f'DEFAULT_OBS_TAG = "{obs}"\nraise RuntimeError("Do not execute source")\n',
            encoding="utf-8")
        return root
    return make


@pytest.fixture
def source(source_factory):
    return source_factory()


@pytest.fixture
def plan():
    return {
        "schema": 1, "repository": REPOSITORY, "upstream_repository": "jonata/pylibobs",
        "source_sha": SOURCE_SHA, "recipe_sha": RECIPE_SHA, "upstream_version": PUBLIC_VERSION,
        "obs_version": "33.0.0", "build_revision": 7, "version": SNAPSHOT_VERSION,
        "tag": SNAPSHOT_TAG, "comparison_sha": BASE_SHA, "comparison_label": PREVIOUS_TAG,
        "run_url": f"https://github.com/{REPOSITORY}/actions/runs/12345",
    }


@pytest.fixture
def planning_api(monkeypatch, mock_api, previous_provenance):
    monkeypatch.setattr(release.subprocess, "check_output", Mock(return_value=SOURCE_SHA + "\n"))
    responses = {
        f"{REPOSITORY}/releases/tags/{SNAPSHOT_TAG}": None,
        f"{REPOSITORY}/git/ref/tags/{SNAPSHOT_TAG}": None,
        f"{REPOSITORY}/releases?per_page=100&page=1": [previous_release()],
        f"jonata/pylibobs/compare/{BASE_SHA}...{SOURCE_SHA}": {"status": "ahead"},
    }
    mock_api(responses)
    return responses


@pytest.mark.parametrize("sha", ["", "main", "a" * 39, "a" * 41, "A" * 40, "g" * 40, SOURCE_SHA + "\n"])
def test_requires_a_full_lowercase_commit_sha(sha):
    with pytest.raises(ValueError, match="SHA"):
        release.require_sha(sha)


def test_accepts_a_full_commit_sha():
    assert release.require_sha(SOURCE_SHA) == SOURCE_SHA


@pytest.mark.parametrize("revision", ["", "0", "-1", "01", "1.5", "1000000000", "7\n"])
def test_plan_rejects_invalid_revision_before_external_calls(source, revision):
    with pytest.raises(ValueError, match="revision"):
        release.plan_snapshot(source, SOURCE_SHA, revision, REPOSITORY, RECIPE_SHA)
    release.subprocess.check_output.assert_not_called()


@pytest.mark.parametrize("source_sha,recipe_sha", [("main", RECIPE_SHA), (SOURCE_SHA, "abc123")])
def test_plan_validates_both_source_and_recipe_commits(source, source_sha, recipe_sha):
    with pytest.raises(ValueError, match="SHA"):
        release.plan_snapshot(source, source_sha, "7", REPOSITORY, recipe_sha)
    release.subprocess.check_output.assert_not_called()


def test_plan_pins_source_recipe_comparison_and_local_version(source, planning_api, plan):
    result = release.plan_snapshot(source, SOURCE_SHA, "7", REPOSITORY, RECIPE_SHA)
    assert result == plan
    release.subprocess.check_output.assert_called_once_with(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True)
    assert (source / "pyproject.toml").read_text(encoding="utf-8").count(PUBLIC_VERSION) == 2


def test_plan_refuses_a_different_checkout(source, planning_api):
    release.subprocess.check_output.return_value = BASE_SHA
    with pytest.raises(ValueError, match="checkout"):
        release.plan_snapshot(source, SOURCE_SHA, "7", REPOSITORY, RECIPE_SHA)
    release.api.assert_not_called()


@pytest.mark.parametrize("version,package_version,obs", [
    ("33.0.0+local", None, "33.0.0"),
    ("33.0.0rc1", None, "33.0.0"),
    ("33.0.0.0", None, "33.0.0"),
    (PUBLIC_VERSION, "33.0.0.1", "33.0.0"),
    (PUBLIC_VERSION, None, "32.2.2"),
])
def test_plan_refuses_nonpublic_or_inconsistent_source_versions(
    source_factory, planning_api, version, package_version, obs,
):
    source = source_factory(version, package_version, obs)
    with pytest.raises(ValueError):
        release.plan_snapshot(source, SOURCE_SHA, "7", REPOSITORY, RECIPE_SHA)
    release.api.assert_not_called()


@pytest.mark.parametrize("version", ["33.0.0", PUBLIC_VERSION])
def test_source_versions_accepts_obs_and_binding_only_releases_without_importing(source_factory, version):
    assert release.source_versions(source_factory(version)) == (version, "33.0.0")


@pytest.mark.parametrize("status", ["ahead", "identical", "behind", "diverged"])
def test_plan_requires_baseline_ancestry(source, planning_api, status):
    planning_api[f"jonata/pylibobs/compare/{BASE_SHA}...{SOURCE_SHA}"] = {"status": status}
    if status in ("ahead", "identical"):
        assert release.plan_snapshot(source, SOURCE_SHA, "7", REPOSITORY, RECIPE_SHA)["comparison_sha"] == BASE_SHA
    else:
        with pytest.raises(ValueError, match="ancestor"):
            release.plan_snapshot(source, SOURCE_SHA, "7", REPOSITORY, RECIPE_SHA)


def test_first_snapshot_uses_the_pypi_version_tag_not_upstream_head(monkeypatch, mock_api):
    mock_api({
        f"{REPOSITORY}/releases?per_page=100&page=1": [
            previous_release(draft=True), previous_release(tag_name="v33.0.0.9"),
        ],
        "jonata/pylibobs/commits/v33.0.0.1": {"sha": BASE_SHA},
    })
    pypi = Mock(return_value={"info": {"version": "33.0.0.1"}})
    monkeypatch.setattr(release, "request_json", pypi)
    assert release.comparison_base(REPOSITORY) == (BASE_SHA, "v33.0.0.1")
    pypi.assert_called_once_with("https://pypi.org/pypi/pylibobs/json")


def test_latest_published_snapshot_can_be_on_the_last_page(mock_api, previous_provenance):
    first_page = [previous_release(tag_name=f"v33.0.0.{index}") for index in range(98)]
    first_page += [
        previous_release(tag_name="snapshot-future-draft", draft=True, published_at="2099-01-01T00:00:00Z"),
        previous_release(tag_name="snapshot-older", published_at="2026-09-01T00:00:00Z"),
    ]
    mock_api({
        f"{REPOSITORY}/releases?per_page=100&page=1": first_page,
        f"{REPOSITORY}/releases?per_page=100&page=2": [
            previous_release(),
            previous_release(tag_name="snapshot-oldest", published_at="2026-08-01T00:00:00Z"),
        ],
    })
    assert release.comparison_base(REPOSITORY) == (BASE_SHA, PREVIOUS_TAG)
    release.request_json.assert_called_once_with(MANIFEST_URL)


@pytest.mark.parametrize("assets", [[], [
    {"name": "provenance.json", "browser_download_url": MANIFEST_URL},
    {"name": "provenance.json", "browser_download_url": MANIFEST_URL},
]])
def test_unknown_snapshot_source_cannot_fall_back_to_notes_or_pypi(mock_api, assets):
    mock_api({f"{REPOSITORY}/releases?per_page=100&page=1": [previous_release(assets=assets)]})
    with pytest.raises(ValueError, match="source"):
        release.comparison_base(REPOSITORY)


@pytest.mark.parametrize("field,value", [
    ("schema", 2), ("upstream_repository", "other/pylibobs"),
    ("tag", "snapshot-unrelated"), ("source_sha", "abc123"),
])
def test_previous_snapshot_requires_matching_valid_provenance(mock_api, previous_provenance, field, value):
    previous_provenance[field] = value
    mock_api({f"{REPOSITORY}/releases?per_page=100&page=1": [previous_release()]})
    with pytest.raises(ValueError):
        release.comparison_base(REPOSITORY)


@pytest.mark.parametrize("existing", ["releases/tags", "git/ref/tags"])
def test_existing_release_or_tag_prevents_planning(source, planning_api, existing):
    planning_api[f"{REPOSITORY}/{existing}/{SNAPSHOT_TAG}"] = {"id": 42}
    with pytest.raises(ValueError, match="already exists"):
        release.plan_snapshot(source, SOURCE_SHA, "7", REPOSITORY, RECIPE_SHA)
    release.request_json.assert_not_called()


@pytest.mark.parametrize("status,missing_ok", [(404, True), (404, False), (403, True), (500, True)])
def test_api_only_treats_optional_404_as_absence(monkeypatch, status, missing_ok):
    error = HTTPError("https://api.github.com/test", status, "failure", {}, None)
    monkeypatch.setattr(release, "urlopen", Mock(side_effect=error))
    if status == 404 and missing_ok:
        assert release.api(f"{REPOSITORY}/releases/tags/{SNAPSHOT_TAG}", missing_ok=True) is None
    else:
        with pytest.raises(HTTPError) as raised:
            release.api(f"{REPOSITORY}/releases/tags/{SNAPSHOT_TAG}", missing_ok=missing_ok)
        assert raised.value is error


def source_contents(source):
    return {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}


def test_stamp_changes_only_the_two_version_fields(source, plan):
    before = source_contents(source)
    release.stamp_version(source, plan)
    expected = dict(before)
    for relative, assignment in [("pyproject.toml", "version"), ("pylibobs/__init__.py", "__version__")]:
        expected[Path(relative)] = before[Path(relative)].replace(
            f'{assignment} = "{PUBLIC_VERSION}"'.encode(),
            f'{assignment} = "{SNAPSHOT_VERSION}"'.encode(),
        )
    assert source_contents(source) == expected


@pytest.mark.parametrize("relative,assignment", [
    ("pyproject.toml", "version"), ("pylibobs/__init__.py", "__version__"),
])
@pytest.mark.parametrize("malformation", ["unmatched", "duplicate"])
def test_stamp_validates_both_assignments_before_writing(source, plan, relative, assignment, malformation):
    path = source / relative
    text = path.read_text(encoding="utf-8")
    if malformation == "unmatched":
        text = text.replace(f'{assignment} = "{PUBLIC_VERSION}"', f"{assignment} = '{PUBLIC_VERSION}'")
    elif relative == "pyproject.toml":
        text += f'\n[tool.example]\nversion = "{PUBLIC_VERSION}"\n'
    else:
        text += f'{assignment} = "{PUBLIC_VERSION}"\n'
    path.write_text(text, encoding="utf-8")
    before = source_contents(source)
    with pytest.raises(ValueError, match="exactly one"):
        release.stamp_version(source, plan)
    assert source_contents(source) == before


def test_stamp_refuses_a_plan_for_a_different_public_version(source, plan):
    plan["upstream_version"] = "33.0.0.1"
    before = source_contents(source)
    with pytest.raises(ValueError, match="plan"):
        release.stamp_version(source, plan)
    assert source_contents(source) == before


@pytest.fixture
def wheel_factory(tmp_path, plan):
    def make(target, *, omit=(), empty=(), metadata_version=None, filename_version=None, platform_tag=None):
        platform, arch = target.split("-", 1)
        files = {f"pylibobs/_libs/{platform}/{arch}/{name}": b"synthetic runtime" for name in RUNTIME_FILES[platform]}
        if platform == "macos":
            files.update({f"pylibobs/native/{name}.dylib": b"synthetic helper" for name in ("macos_surfaces", "macos_camera")})
        metadata = f"pylibobs-{plan['version']}.dist-info/METADATA"
        files[metadata] = (
            f"Metadata-Version: 2.1\nName: pylibobs\nVersion: {metadata_version or plan['version']}\n\n"
        ).encode()
        for name in omit:
            del files[name]
        for name in empty:
            files[name] = b""
        dist = tmp_path / "dist"
        dist.mkdir(exist_ok=True)
        path = dist / f"pylibobs-{filename_version or plan['version']}-py3-none-{platform_tag or PLATFORM_TAGS[target]}.whl"
        with zipfile.ZipFile(path, "w") as archive:
            for name, contents in files.items():
                archive.writestr(name, contents)
        return path
    return make


@pytest.mark.parametrize("target", PLATFORM_TAGS)
def test_inspect_accepts_each_complete_supported_wheel(wheel_factory, plan, target):
    release.inspect_wheel(wheel_factory(target), plan, target)


@pytest.mark.parametrize("target,missing", [
    ("windows-x86_64", "pylibobs/_libs/windows/x86_64/obs.dll"),
    ("windows-x86_64", "pylibobs/_libs/windows/x86_64/core/win-dshow/data/obs-virtualcam-module32.dll"),
    ("linux-x86_64", "pylibobs/_libs/linux/x86_64/data/libobs/default.effect"),
    ("macos-arm64", "pylibobs/_libs/macos/arm64/runtime.json"),
    ("macos-arm64", "pylibobs/native/macos_surfaces.dylib"),
    ("macos-x86_64", "pylibobs/native/macos_camera.dylib"),
])
@pytest.mark.parametrize("damage", ["missing", "empty"])
def test_inspect_rejects_missing_or_empty_runtime_and_helpers(wheel_factory, plan, target, missing, damage):
    wheel = wheel_factory(target, **{"omit" if damage == "missing" else "empty": [missing]})
    with pytest.raises(ValueError, match="runtime"):
        release.inspect_wheel(wheel, plan, target)


@pytest.mark.parametrize("mismatch", ["metadata", "filename", "target", "unsupported"])
def test_inspect_rejects_wrong_version_or_target(wheel_factory, plan, mismatch):
    options = {
        "metadata": {"metadata_version": PUBLIC_VERSION},
        "filename": {"filename_version": PUBLIC_VERSION},
        "target": {},
        "unsupported": {"platform_tag": "linux_x86_64"},
    }
    wheel = wheel_factory("windows-x86_64", **options[mismatch])
    target = "macos-arm64" if mismatch == "target" else "windows-x86_64"
    with pytest.raises(ValueError):
        release.inspect_wheel(wheel, plan, target)


@pytest.mark.parametrize("count", [0, 2])
def test_inspect_requires_unambiguous_distribution_metadata(wheel_factory, plan, count):
    metadata = f"pylibobs-{plan['version']}.dist-info/METADATA"
    wheel = wheel_factory("linux-x86_64", omit=[metadata] if count == 0 else [])
    if count == 2:
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr("other-1.0.dist-info/METADATA", "Name: other\nVersion: 1.0\n")
    with pytest.raises(ValueError, match="metadata"):
        release.inspect_wheel(wheel, plan, "linux-x86_64")


def test_assemble_four_targets_with_verifiable_checksums_and_provenance_links(wheel_factory, plan):
    wheels = [wheel_factory(target) for target in PLATFORM_TAGS]
    dist = wheels[0].parent
    release.assemble(dist, plan)
    provenance = json.loads((dist / "provenance.json").read_text(encoding="utf-8"))
    hashes = {wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest() for wheel in wheels}
    assert provenance == dict(plan, artifacts=hashes)
    lines = (dist / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    checksums = {name: checksum for checksum, name in (line.split("  ", 1) for line in lines)}
    assets = [*wheels, dist / "provenance.json"]
    assert len(lines) == len(assets)
    assert checksums == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in assets}
    notes = (dist.parent / "release-notes.md").read_text(encoding="utf-8")
    for link in (
        f"https://github.com/jonata/pylibobs/commit/{SOURCE_SHA}",
        f"https://github.com/jonata/pylibobs/compare/{BASE_SHA}...{SOURCE_SHA}", plan["run_url"],
    ):
        assert f"({link})" in notes


@pytest.mark.parametrize("damage", ["missing", "duplicate", "unsupported", "corrupt"])
def test_assemble_refuses_incomplete_duplicate_or_invalid_wheels_without_manifests(wheel_factory, plan, damage):
    wheels = {target: wheel_factory(target) for target in PLATFORM_TAGS}
    dist = wheels["linux-x86_64"].parent
    if damage in ("missing", "duplicate"):
        wheels["macos-x86_64"].unlink()
        if damage == "duplicate":
            wheel_factory("linux-x86_64", platform_tag="manylinux_2_40_x86_64")
    elif damage == "unsupported":
        wheel_factory("linux-x86_64", platform_tag="musllinux_1_2_x86_64")
    else:
        wheel_factory("macos-arm64", omit=["pylibobs/native/macos_camera.dylib"])
    with pytest.raises(ValueError):
        release.assemble(dist, plan)
    assert not (dist / "provenance.json").exists()
    assert not (dist / "SHA256SUMS").exists()
    assert not (dist.parent / "release-notes.md").exists()


@pytest.fixture
def publication(tmp_path, mock_api, monkeypatch):
    mock_api({
        f"{REPOSITORY}/releases/tags/{SNAPSHOT_TAG}": None,
        f"{REPOSITORY}/git/ref/tags/{SNAPSHOT_TAG}": None,
    })
    run = Mock()
    monkeypatch.setattr(release.subprocess, "run", run)
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in ["windows.whl", "linux.whl", "macos-arm.whl", "macos-intel.whl", "provenance.json", "SHA256SUMS"]:
        (dist / name).write_bytes(b"synthetic asset")
    (tmp_path / "release-notes.md").write_text("Snapshot notes", encoding="utf-8")
    return dist, run


def test_publish_uploads_all_assets_to_a_draft_before_finalizing(publication, plan):
    dist, run = publication
    release.publish(dist, plan)
    assert run.call_count == 2
    create_call, finalize_call = run.call_args_list
    create, finalize = create_call.args[0], finalize_call.args[0]
    assert create[:4] == ["gh", "release", "create", SNAPSHOT_TAG]
    assert set(create[4:create.index("--repo")]) == {str(path) for path in dist.iterdir()}
    assert create[create.index("--target") + 1] == RECIPE_SHA
    assert create[create.index("--notes-file") + 1] == str(dist.parent / "release-notes.md")
    assert "--draft" in create
    assert finalize[:4] == ["gh", "release", "edit", SNAPSHOT_TAG]
    assert "--draft=false" in finalize
    for command in (create, finalize):
        assert command[command.index("--repo") + 1] == REPOSITORY
        assert "--prerelease" in command and "--latest=false" in command
    assert create_call.kwargs["check"] is True
    assert finalize_call.kwargs["check"] is True


@pytest.mark.parametrize("failure", ["creation", "upload"])
def test_publish_never_finalizes_when_draft_creation_or_upload_fails(publication, plan, failure):
    dist, run = publication
    error = FileNotFoundError("gh unavailable") if failure == "creation" else subprocess.CalledProcessError(1, "gh release create")
    run.side_effect = error
    with pytest.raises(type(error)) as raised:
        release.publish(dist, plan)
    assert raised.value is error
    assert run.call_count == 1
    assert run.call_args.args[0][:3] == ["gh", "release", "create"]


@pytest.mark.parametrize("environment,value", [("GITHUB_REPOSITORY", "other/pylibobs"), ("GITHUB_SHA", BASE_SHA)])
def test_publish_refuses_repository_or_recipe_mismatch_before_side_effects(publication, plan, monkeypatch, environment, value):
    dist, run = publication
    monkeypatch.setenv(environment, value)
    with pytest.raises(ValueError, match="match the plan"):
        release.publish(dist, plan)
    release.api.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("existing", ["releases/tags", "git/ref/tags"])
def test_publish_rechecks_release_and_tag_collisions(publication, plan, mock_api, existing):
    dist, run = publication
    mock_api({
        f"{REPOSITORY}/releases/tags/{SNAPSHOT_TAG}": {"id": 42} if existing == "releases/tags" else None,
        f"{REPOSITORY}/git/ref/tags/{SNAPSHOT_TAG}": {"ref": SNAPSHOT_TAG} if existing == "git/ref/tags" else None,
    })
    with pytest.raises(ValueError, match="already exists"):
        release.publish(dist, plan)
    run.assert_not_called()
