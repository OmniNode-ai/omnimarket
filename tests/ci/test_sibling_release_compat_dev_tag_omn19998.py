# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19998 - the Sibling Release Compatibility Gate accepts a dev tag pin.

RULING 2026-10-16T09:35:49Z: in the lab, tags replace releases. Each sibling's
``dev-merge-tag.yml`` tags every dev push ``dev-<UTC>-<sha12>``, so
``sibling-release-compat`` must accept a ``uv.lock`` git source pinned to such a
tag as well as a PyPI release, still checking out the exact pinned revision, and
must never widen to an unpinned branch.

The tests run the gate's own steps, extracted from ``ci.yml``: the pins step's
Python against fixture lock files, and the tag step's shell against a local
remote that stands in for the sibling repository. Every refusal has a positive
control beside it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
JOB_ID = "sibling-release-compat"
INFRA_REPO = "OmniNode-ai/omnibase_infra"
INFRA_SHA = "1" * 40
CORE_SHA = "2" * 40
INFRA_TAG = f"dev-20261016143012-{INFRA_SHA[:12]}"
CORE_TAG = f"dev-20261016150000-{CORE_SHA[:12]}"
PYPI = '{ registry = "https://pypi.org/simple" }'


def _steps() -> list[dict[str, Any]]:
    parsed = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    steps = parsed["jobs"][JOB_ID]["steps"]
    assert isinstance(steps, list)
    return steps


def _step(step_id: str) -> dict[str, Any]:
    matches = [step for step in _steps() if step.get("id") == step_id]
    assert len(matches) == 1, f"expected one step with id {step_id!r}"
    return matches[0]


def _step_named(prefix: str) -> dict[str, Any]:
    matches = [
        step for step in _steps() if str(step.get("name", "")).startswith(prefix)
    ]
    assert len(matches) == 1, f"expected one step named {prefix!r}..."
    return matches[0]


def _pins_python() -> str:
    run = _step("pins")["run"]
    match = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY\n", run, re.DOTALL)
    assert match, "pins step has no python heredoc"
    lines = match.group(1).splitlines()
    indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indent:] for line in lines) + "\n"


def _lock(
    infra_source: str, core_source: str = PYPI, *, infra_version: str = "0.38.73"
) -> str:
    return (
        "version = 1\n\n"
        '[[package]]\nname = "omnibase-infra"\n'
        f'version = "{infra_version}"\nsource = {infra_source}\n\n'
        '[[package]]\nname = "omnibase-core"\n'
        f'version = "0.47.44"\nsource = {core_source}\n'
    )


def _git_source(
    *,
    repo: str = INFRA_REPO,
    query: str = f"tag={INFRA_TAG}",
    sha: str = INFRA_SHA,
    suffix: str = "",
) -> str:
    return f'{{ git = "https://github.com/{repo}{suffix}?{query}#{sha}" }}'


def _run_pins(
    tmp_path: Path, lock: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-c", _pins_python()],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        check=False,
    )
    outputs = dict(
        line.split("=", 1)
        for line in output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return result, outputs


def test_a_pypi_release_pin_checks_out_its_v_tag_as_before(tmp_path: Path) -> None:
    result, out = _run_pins(tmp_path, _lock(PYPI, infra_version="0.38.72"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert out["infra_version"] == "0.38.72"
    assert out["infra_ref"] == "v0.38.72"
    assert out["infra_ref_name"] == "v0.38.72"
    assert out["infra_dev_tag"] == ""
    assert out["infra_spec"] == "omnibase-infra==0.38.72"
    assert out["core_ref"] == "v0.47.44"
    assert out["core_spec"] == "omnibase-core==0.47.44"


@pytest.mark.parametrize("suffix", ["", ".git"])
def test_a_dev_tag_git_pin_resolves_to_the_pinned_commit(
    tmp_path: Path, suffix: str
) -> None:
    result, out = _run_pins(tmp_path, _lock(_git_source(suffix=suffix)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert out["infra_version"] == "0.38.73"
    assert out["infra_ref"] == INFRA_SHA, (
        "the exact pinned commit is checked out, not the tag name"
    )
    assert out["infra_ref_name"] == INFRA_TAG
    assert out["infra_dev_tag"] == INFRA_TAG
    assert (
        out["infra_spec"]
        == f"omnibase-infra @ git+https://github.com/{INFRA_REPO}.git@{INFRA_SHA}"
    )
    assert out["core_ref"] == "v0.47.44", "the other sibling stays on its release"


def test_core_may_follow_a_dev_tag_too(tmp_path: Path) -> None:
    core = _git_source(
        repo="OmniNode-ai/omnibase_core", query=f"tag={CORE_TAG}", sha=CORE_SHA
    )
    result, out = _run_pins(tmp_path, _lock(PYPI, core))
    assert result.returncode == 0, result.stdout + result.stderr
    assert out["core_ref"] == CORE_SHA
    assert out["core_dev_tag"] == CORE_TAG


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("an unpinned branch", _git_source(query="branch=dev")),
        (
            "a branch that is the default",
            '{ git = "https://github.com/OmniNode-ai/omnibase_infra#'
            + INFRA_SHA
            + '" }',
        ),
        ("a bare rev", _git_source(query=f"rev={INFRA_SHA}")),
        ("a rev spelled like a dev tag", _git_source(query=f"rev={INFRA_TAG}")),
        ("a release tag", _git_source(query="tag=v0.38.73")),
        (
            "a dev tag that names another commit",
            _git_source(query=f"tag=dev-20261016143012-{'f' * 12}"),
        ),
        ("a malformed dev tag", _git_source(query="tag=dev-latest")),
        ("a dev tag of another repository", _git_source(repo="OmniNode-ai/omnimarket")),
        ("a fork", _git_source(repo="someone/omnibase_infra")),
        (
            "a subdirectory source",
            '{ git = "https://github.com/OmniNode-ai/omnibase_infra?tag='
            + INFRA_TAG
            + "#"
            + INFRA_SHA
            + '", subdirectory = "x" }',
        ),
        ("a directory source", '{ directory = "../omnibase_infra" }'),
        ("an editable source", '{ editable = "." }'),
        (
            "a registry that is not PyPI",
            '{ registry = "https://example.invalid/simple" }',
        ),
    ],
)
def test_anything_else_is_still_refused(
    tmp_path: Path, label: str, source: str
) -> None:
    result, out = _run_pins(tmp_path, _lock(source))
    assert result.returncode != 0, f"{label} must be refused:\n{result.stdout}"
    assert "::error::omnibase-infra" in result.stdout
    assert out == {}, "a refused lock must publish no pins"


def test_a_missing_package_entry_is_refused(tmp_path: Path) -> None:
    lock = (
        _lock(PYPI).split("[[package]]")[0]
        + '[[package]]\nname = "omnibase-core"\nversion = "0.47.44"\nsource = '
        + PYPI
        + "\n"
    )
    result, _ = _run_pins(tmp_path, lock)
    assert result.returncode != 0
    assert "entries=0" in result.stdout


# --- the tag step -----------------------------------------------------------


def _git_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in scrub_git_location_env(os.environ).items()
        if not key.startswith("GIT_")
    }
    env.update(
        {
            "GIT_AUTHOR_NAME": "sibling gate test",
            "GIT_AUTHOR_EMAIL": "sibling-gate@example.invalid",
            "GIT_COMMITTER_NAME": "sibling gate test",
            "GIT_COMMITTER_EMAIL": "sibling-gate@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return env


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=scrub_git_location_env(_git_env()),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def sibling(tmp_path: Path) -> tuple[Path, str]:
    """A bare repository standing in for omnibase_infra, and its one commit."""
    origin = tmp_path / "omnibase_infra.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-q", str(origin))
    _git(tmp_path, "init", "-q", "-b", "dev", str(work))
    (work / "README").write_text("sibling fixture\n", encoding="utf-8")
    _git(work, "add", "README")
    _git(work, "commit", "-q", "-m", "fixture")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "dev")
    return origin, _git(work, "rev-parse", "HEAD")


def _run_tags(
    origin: Path, *, infra_tag: str, infra_sha: str
) -> subprocess.CompletedProcess[str]:
    env = _git_env()
    env.update(
        {
            "INFRA_TAG": infra_tag,
            "INFRA_SHA": infra_sha,
            "CORE_TAG": "",
            "CORE_SHA": "v0.47.44",
        }
    )
    # scrub_git_location_env strips every GIT_CONFIG* override, so the redirect
    # of the sibling's URL to the local bare repository rides in a HOME config.
    home = origin.parent / "home"
    home.mkdir(exist_ok=True)
    (home / ".gitconfig").write_text(
        f'[url "{origin}"]\n\tinsteadOf = https://github.com/{INFRA_REPO}.git\n',
        encoding="utf-8",
    )
    env["HOME"] = str(home)
    return subprocess.run(
        ["bash", "-c", _step("tags")["run"]],
        env=scrub_git_location_env(env),
        capture_output=True,
        text=True,
        check=False,
    )


def _tag(origin: Path, name: str, sha: str, *, annotated: bool = False) -> None:
    if annotated:
        _git(origin, "tag", "-a", "-m", "annotated", name, sha)
    else:
        _git(origin, "tag", name, sha)


def test_a_dev_tag_that_exists_and_names_the_pinned_commit_passes(
    sibling: tuple[Path, str],
) -> None:
    origin, sha = sibling
    tag = f"dev-20261016143012-{sha[:12]}"
    _tag(origin, tag, sha)
    result = _run_tags(origin, infra_tag=tag, infra_sha=sha)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        f"OK: omnibase_infra dev tag {tag} names the pinned commit {sha}"
        in result.stdout
    )


def test_an_annotated_dev_tag_is_read_through_to_its_commit(
    sibling: tuple[Path, str],
) -> None:
    origin, sha = sibling
    tag = f"dev-20261016143012-{sha[:12]}"
    _tag(origin, tag, sha, annotated=True)
    result = _run_tags(origin, infra_tag=tag, infra_sha=sha)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_pin_to_a_nonexistent_tag_fails(sibling: tuple[Path, str]) -> None:
    """Negative control: the commit exists, the tag the lock names does not."""
    origin, sha = sibling
    real = f"dev-20261016143012-{sha[:12]}"
    _tag(origin, real, sha)
    missing = f"dev-20261016999999-{sha[:12]}"
    result = _run_tags(origin, infra_tag=missing, infra_sha=sha)
    assert result.returncode != 0
    assert "has no such tag" in result.stdout
    assert missing in result.stdout


def test_a_tag_that_names_another_commit_fails(sibling: tuple[Path, str]) -> None:
    origin, sha = sibling
    tag = f"dev-20261016143012-{sha[:12]}"
    _tag(origin, tag, sha)
    result = _run_tags(origin, infra_tag=tag, infra_sha="a" * 40)
    assert result.returncode != 0
    assert f"the tag names {sha}" in result.stdout


def test_a_release_pin_has_no_dev_tag_to_verify(sibling: tuple[Path, str]) -> None:
    origin, _ = sibling
    result = _run_tags(origin, infra_tag="", infra_sha="v0.38.72")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pinned to a PyPI release" in result.stdout


# --- the rest of the job follows the pin ------------------------------------


def test_the_checkout_install_and_migration_steps_use_the_pinned_revision() -> None:
    checkout = _step_named("Checkout the pinned omnibase_infra revision")
    assert checkout["with"]["ref"] == "${{ steps.pins.outputs.infra_ref }}"
    assert checkout["with"]["repository"] == INFRA_REPO

    migration = _step_named("Every node migration this change adds")
    assert migration["env"]["INFRA_REF"] == "${{ steps.pins.outputs.infra_ref }}"
    assert 'ls-tree "$INFRA_REF"' in migration["run"]
    assert 'ls-tree "v$INFRA_VERSION"' not in migration["run"]

    install = _step("install")
    assert '"$wheel" "$INFRA_SPEC" "$CORE_SPEC"' in install["run"]
    assert "steps.tags.outcome == 'success'" in install["if"]
    assert "direct_url.json" in install["run"], (
        "a dev pin's installed commit is checked"
    )


def test_the_job_still_runs_the_same_compatibility_checks() -> None:
    names = [str(step.get("name", "")) for step in _steps()]
    assert any(name.startswith("Every omnimarket module imports") for name in names)
    assert any(name.startswith("In-process onex delegate smoke") for name in names)
    assert any(name.startswith("Build this head's wheel") for name in names)
