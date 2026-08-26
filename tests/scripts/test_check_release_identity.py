# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the release-identity gate (OMN-16344).

The gate forbids merging a packaged-source change onto an already-published
version string. It is the omnimarket port of the same gate in omnibase_infra
(OMN-13412) and omnibase_core (OMN-13411), and the recurrence guard for the
state this repo was actually found in: dev's ``project.version`` sitting at
0.4.8 — identical to the published v0.4.8 tag — while carrying seven commits of
``src/`` changes, so one version string named two distinct code states.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.version import Version

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_release_identity.py"

# Git environment variables that OVERRIDE both an explicit `--git-dir` flag and
# `cwd` (the OMN-14891 corruption class). Mirrors the local scrub idiom already
# used by handler_report_anchor_probe rather than importing
# omnibase_core.validators.no_unguarded_git_subprocess: that module is a
# test-scanning validator, and this repo's convention is to keep the remedy
# local instead of taking a runtime dependency on it.
_GIT_LOCATION_ENV_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)
_GIT_DISCOVERY_ENV_VARS: tuple[str, ...] = ("GIT_CEILING_DIRECTORIES",)


def _scrub_git_location_env() -> dict[str, str]:
    """Return a copy of the process env with git-location overrides removed."""
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("GIT_CONFIG"):
            del env[key]
    for key in (*_GIT_LOCATION_ENV_VARS, *_GIT_DISCOVERY_ENV_VARS):
        env.pop(key, None)
    return env


def _load_module():
    spec = importlib.util.spec_from_file_location("check_release_identity", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod():
    return _load_module()


@pytest.mark.unit
def test_passes_when_version_ahead_of_published(mod, monkeypatch):
    """src/** changed, but the version is strictly ahead — gate passes."""
    monkeypatch.setattr(mod, "_read_pyproject_version", lambda: Version("0.4.9"))
    monkeypatch.setattr(mod, "_latest_published_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_packaged_source_changed", lambda *_args: True)
    assert mod.main(["--base", "origin/dev"]) == 0


@pytest.mark.unit
def test_fails_when_src_changed_and_version_equals_published(mod, monkeypatch):
    """src/** changed and the version equals the published wheel — gate FAILS.

    This is the exact state omnimarket dev was in before OMN-16344: seven
    commits of src/ changes sitting on 0.4.8, the same version as the published
    v0.4.8 wheel.
    """
    monkeypatch.setattr(mod, "_read_pyproject_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_latest_published_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_packaged_source_changed", lambda *_args: True)
    assert mod.main(["--base", "origin/dev"]) == 1


@pytest.mark.unit
def test_fails_when_src_changed_and_version_behind_published(mod, monkeypatch):
    """A version BEHIND the latest published tag is also a fail."""
    monkeypatch.setattr(mod, "_read_pyproject_version", lambda: Version("0.4.7"))
    monkeypatch.setattr(mod, "_latest_published_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_packaged_source_changed", lambda *_args: True)
    assert mod.main(["--base", "origin/dev"]) == 1


@pytest.mark.unit
def test_exempt_when_no_packaged_source_changed(mod, monkeypatch):
    """A docs/tests/CI-only diff is exempt — the published wheel is unaffected."""
    monkeypatch.setattr(mod, "_read_pyproject_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_latest_published_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_packaged_source_changed", lambda *_args: False)
    assert mod.main(["--base", "origin/dev"]) == 0


@pytest.mark.unit
def test_passes_when_no_published_tag_yet(mod, monkeypatch):
    """A repo with no published tags cannot alias a published version."""
    monkeypatch.setattr(mod, "_read_pyproject_version", lambda: Version("0.1.0"))
    monkeypatch.setattr(mod, "_latest_published_version", lambda: None)
    assert mod.main(["--base", "origin/dev"]) == 0


@pytest.mark.unit
def test_config_error_on_missing_version(mod, monkeypatch):
    """A missing project.version is a config error (exit 2), not a pass."""

    def _raise():
        raise ValueError("no project.version")

    monkeypatch.setattr(mod, "_read_pyproject_version", _raise)
    assert mod.main(["--base", "origin/dev"]) == 2


@pytest.mark.unit
def test_packaged_source_changed_detects_src_prefix(mod):
    """The src/ prefix triggers the bump requirement; non-src does not."""
    assert mod._packaged_source_changed(None, ["src/omnimarket/nodes/node_x/foo.py"])
    assert not mod._packaged_source_changed(
        None, ["docs/foo.md", "tests/test_x.py", ".github/workflows/ci.yml"]
    )


@pytest.mark.unit
def test_explicit_changed_file_overrides_base(mod, monkeypatch):
    """An explicit --changed-file list bypasses git diffing entirely."""
    monkeypatch.setattr(mod, "_read_pyproject_version", lambda: Version("0.4.8"))
    monkeypatch.setattr(mod, "_latest_published_version", lambda: Version("0.4.8"))
    # Explicit src file => changed => must be ahead => fails at 0.4.8.
    assert mod.main(["--changed-file", "src/omnimarket/foo.py"]) == 1
    # Explicit docs file => not changed => exempt => passes.
    assert mod.main(["--changed-file", "docs/foo.md"]) == 0


def _isolated_checkout(tmp_path: Path, *, published_tag: str) -> Path:
    """Stand the REAL script up in a throwaway repo whose tag set we own.

    ``check_release_identity`` derives its repo root from its own file location
    (``Path(__file__).resolve().parents[1]``) and shells out to ``git`` there,
    so copying the real script + the real ``pyproject.toml`` into
    ``<tmp>/scripts/`` + ``<tmp>/`` makes ``<tmp>`` the root it inspects. Every
    input the gate reads is then under the test's control — no tags are written
    into, or deleted from, the developer's actual checkout, and the assertions
    stay valid no matter which real version omnimarket is sitting on.
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(_SCRIPT, root / "scripts" / _SCRIPT.name)
    shutil.copy2(
        _SCRIPT.resolve().parents[1] / "pyproject.toml", root / "pyproject.toml"
    )

    # The identity and signing overrides are pinned per-command for the same
    # reason the env is scrubbed: the throwaway repo must not inherit the
    # developer's global git config. `_scrub_git_location_env` only clears
    # LOCATION variables, so a developer with `tag.gpgsign = true` set globally
    # would have this lightweight tag demand a message and die with
    # "fatal: no tag message?" (exit 128) -- a failure that never reproduces in
    # CI, which signs nothing.
    scrubbed_git_env = _scrub_git_location_env()
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "smoke",
        ],
        ["-c", "tag.gpgsign=false", "tag", published_tag],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, env=scrubbed_git_env)
    return root


def _run_gate(root: Path) -> subprocess.CompletedProcess[str]:
    """Run the gate against ``root`` only.

    The scrub is load-bearing, not ceremony: GIT_DIR / GIT_WORK_TREE override
    ``cwd``, so an ambient one (a git hook exports exactly these — the
    OMN-14891 case) would make the gate's internal ``git tag --list`` read the
    developer's real checkout instead of the isolated repo, and the isolation
    this helper exists to provide would silently evaporate.
    """
    scrubbed_git_env = _scrub_git_location_env()
    return subprocess.run(
        [sys.executable, str(root / "scripts" / _SCRIPT.name)],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
        env=scrubbed_git_env,
    )


@pytest.mark.unit
def test_live_invocation_smoke(tmp_path):
    """Real subprocess run of the real script, end to end: version ahead => 0.

    The isolated checkout owns its tag set outright rather than forcing a
    synthetic high tag into the real repo. ``_latest_published_version`` takes
    the MAX over all tags, so a synthetic tag only decides the comparison while
    it outranks every real tag — the moment a higher real tag is cut such a
    fixture goes inert and the assertion inverts on every open PR.
    """
    root = _isolated_checkout(tmp_path, published_tag="v0.0.1")

    result = _run_gate(root)

    assert result.returncode == 0, result.stderr
    assert "ahead of latest published" in result.stdout


@pytest.mark.unit
def test_live_invocation_fails_when_version_is_not_ahead(tmp_path):
    """Live negative: the gate must FAIL, not merely be absent, when behind.

    Exists-but-wrong, end to end through the real subprocess — a script that
    silently exited 0 on an un-bumped version would pass the positive smoke
    above and still let the whole failure class through.
    """
    root = _isolated_checkout(tmp_path, published_tag="v99.0.0")

    result = _run_gate(root)

    assert result.returncode == 1, result.stdout
    assert "is NOT ahead of the latest published version" in result.stderr
