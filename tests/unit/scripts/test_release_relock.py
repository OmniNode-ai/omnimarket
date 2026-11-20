# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Release relock proof: a release commit locks released sibling wheels (OMN-26166).

RULING 2026-11-20T11:37:47Z: stability is based on released packages, so a
release commit carries a ``uv.lock`` whose omnibase-infra, omnibase-core and
omnibase-compat entries read ``source = { registry = ... }``. Dev, the
reopen-dev commit and every other commit keep their dev-tag git locks.

``src/omnimarket/handlers/release_relock.py`` holds the pure decision functions; the
``release`` job of ``.github/workflows/release-cut.yml`` is the one step that
builds the release commit, and these tests pin where it sits.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers import release_relock as rr

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_CUT = REPO_ROOT / ".github" / "workflows" / "release-cut.yml"

GIT_INFRA = (
    "https://github.com/OmniNode-ai/omnibase_infra.git"
    "?tag=dev-20261119055404-a679b44e2cfe#a679b44e2cfe2d6148a201273415ba96401f29ed"
)
GIT_CORE = (
    "https://github.com/OmniNode-ai/omnibase_core.git"
    "?tag=dev-20261113013358-a09066caf945#a09066caf945cd79e13447d92ad42549f1019f78"
)
GIT_SPI = (
    "https://github.com/OmniNode-ai/omnibase_spi.git"
    "?tag=dev-20261108170744-52946639a23a#52946639a23afcbaaa81cc81cdfbfafc098ef9a6"
)
PYPI = '{ registry = "https://pypi.org/simple" }'


def _package(name: str, version: str, source: str) -> str:
    return f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {source}\n'


def _lock(infra: str, core: str, compat: str | None) -> str:
    """A lock with the three siblings; ``spi`` stays a git dev tag throughout."""
    blocks = [
        _package("omnibase-compat", "0.5.7", compat) if compat else "",
        _package("omnibase-core", "0.47.73", core),
        _package("omnibase-infra", "0.38.79", infra),
        _package("omnibase-spi", "0.23.15", '{ git = "' + GIT_SPI + '" }'),
        _package("pytest", "9.0.0", PYPI),
    ]
    return "version = 1\n\n" + "\n".join(b for b in blocks if b)


def _git(url: str) -> str:
    return '{ git = "' + url + '" }'


DEV_LOCK = _lock(_git(GIT_INFRA), _git(GIT_CORE), None)
RELEASE_LOCK = _lock(PYPI, PYPI, PYPI)


# --- AC1: the release commit relocks every git-locked sibling -----------------


def test_release_relock_a_release_commit_with_a_git_sibling_is_refused() -> None:
    findings = rr.check_lock(DEV_LOCK, release_commit=True)
    assert {(f.code, f.package) for f in findings} == {
        ("git_source_on_release", "omnibase-infra"),
        ("git_source_on_release", "omnibase-core"),
    }


@pytest.mark.parametrize("sibling", sorted(rr.SIBLINGS))
def test_release_relock_each_git_sibling_alone_refuses_the_release_commit(
    sibling: str,
) -> None:
    sources = {
        "omnibase-infra": PYPI,
        "omnibase-core": PYPI,
        "omnibase-compat": PYPI,
    }
    sources[sibling] = _git(
        "https://github.com/OmniNode-ai/omnibase_x.git?tag=dev-20261119055404-a679b44e2cfe#"
        + "a" * 40
    )
    lock = _lock(
        sources["omnibase-infra"], sources["omnibase-core"], sources["omnibase-compat"]
    )
    findings = rr.check_lock(lock, release_commit=True)
    assert [(f.code, f.package) for f in findings] == [
        ("git_source_on_release", sibling)
    ]


def test_release_relock_a_relocked_release_commit_passes() -> None:
    assert rr.check_lock(RELEASE_LOCK, release_commit=True) == []


def test_release_relock_an_absent_compat_entry_is_not_a_finding() -> None:
    lock = _lock(PYPI, PYPI, None)
    assert rr.check_lock(lock, release_commit=True) == []


def test_release_relock_a_reopen_dev_commit_holding_an_index_wheel_is_refused() -> None:
    findings = rr.check_lock(RELEASE_LOCK, release_commit=False)
    assert {(f.code, f.package) for f in findings} == {
        ("registry_source_off_release", "omnibase-infra"),
        ("registry_source_off_release", "omnibase-core"),
        ("registry_source_off_release", "omnibase-compat"),
    }


def test_release_relock_a_dev_lock_keeps_its_dev_tag_locks() -> None:
    assert rr.check_lock(DEV_LOCK, release_commit=False) == []


def test_release_relock_only_the_three_siblings_are_in_scope() -> None:
    assert (
        frozenset({"omnibase-infra", "omnibase-core", "omnibase-compat"}) == rr.SIBLINGS
    )
    # omnibase-spi stays a git dev tag on a release commit; the ruling covers three.
    assert rr.check_lock(RELEASE_LOCK, release_commit=True) == []


def test_release_relock_strip_sources_drops_only_the_three_sibling_pins() -> None:
    pyproject = (
        "[project]\nname = 'x'\n\n[tool.uv.sources]\n"
        "# keep this comment\n"
        'omnibase-infra = { git = "https://github.com/OmniNode-ai/omnibase_infra.git", tag = "dev-20261119055404-a679b44e2cfe" }\n'
        'omnibase-core = { git = "https://github.com/OmniNode-ai/omnibase_core.git", tag = "dev-20261113013358-a09066caf945" }\n'
        'omnibase-spi = { git = "https://github.com/OmniNode-ai/omnibase_spi.git", tag = "dev-20261108170744-52946639a23a" }\n'
        '\n[tool.other]\nomnibase-infra = "not a source"\n'
    )
    stripped = rr.strip_sources(pyproject)
    assert "omnibase_infra.git" not in stripped
    assert "omnibase_core.git" not in stripped
    assert "omnibase_spi.git" in stripped
    assert "# keep this comment" in stripped
    assert 'omnibase-infra = "not a source"' in stripped


def test_release_relock_marker_round_trips(tmp_path: Path) -> None:
    assert rr.is_release_commit(tmp_path) is False
    (tmp_path / rr.MARKER_NAME).write_text(rr.marker_text("0.4.324"))
    assert rr.is_release_commit(tmp_path) is True
    assert json.loads((tmp_path / rr.MARKER_NAME).read_text())["version"] == "0.4.324"


def test_release_relock_verify_exit_codes(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text(DEV_LOCK)
    assert rr.verify(tmp_path) == 0
    (tmp_path / rr.MARKER_NAME).write_text(rr.marker_text("0.4.324"))
    assert rr.verify(tmp_path) == 1
    (tmp_path / "uv.lock").write_text(RELEASE_LOCK)
    assert rr.verify(tmp_path) == 0


# --- where the release commit is built ---------------------------------------


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(RELEASE_CUT.read_text())


def _steps(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    return list(workflow["jobs"][job]["steps"])


def _index(workflow: dict[str, Any], job: str, needle: str) -> int:
    for i, step in enumerate(_steps(workflow, job)):
        if needle in str(step.get("run", "")):
            return i
    raise AssertionError(f"no step in {job} runs {needle!r}")


def test_release_relock_runs_in_the_release_job_between_the_pin_gate_and_the_tag(
    workflow: dict[str, Any],
) -> None:
    gate = _index(workflow, "release", "verify_pypi_pin_resolvability.py")
    relock = _index(workflow, "release", "release_relock.py")
    tag = _index(workflow, "release", "git push origin")
    assert gate < relock < tag


def test_release_relock_step_relocks_with_uv_and_verifies_the_lock(
    workflow: dict[str, Any],
) -> None:
    run = _steps(workflow, "release")[_index(workflow, "release", "release_relock.py")][
        "run"
    ]
    assert "m.strip_pyproject" in run
    assert "uv lock" in run
    assert "git checkout -- pyproject.toml" in run
    assert "m.verify(" in run
    assert (
        run.index("m.strip_pyproject") < run.index("uv lock") < run.index("m.verify(")
    )


def test_release_relock_the_tag_names_the_release_commit_not_dev_head(
    workflow: dict[str, Any],
) -> None:
    outputs = workflow["jobs"]["release"]["outputs"]
    step = next(
        s
        for s in _steps(workflow, "release")
        if "release_relock.py" in str(s.get("run", ""))
    )
    assert outputs["tag_sha"] == "${{ steps." + step["id"] + ".outputs.sha }}"


def test_release_relock_the_release_commit_descends_from_dev_and_main(
    workflow: dict[str, Any],
) -> None:
    run = _steps(workflow, "release")[_index(workflow, "release", "release_relock.py")][
        "run"
    ]
    # Two parents so the sync-main fast-forward (force=false) still holds on
    # the second release, whose dev parent is not a descendant of the first.
    assert re.search(r"commit-tree .*-p HEAD -p origin/main", run, re.S)
    assert "git push" not in run
    assert "release-commit marker" in run or rr.MARKER_NAME in run


def test_release_relock_reopen_dev_never_relocks(workflow: dict[str, Any]) -> None:
    for step in _steps(workflow, "reopen-dev"):
        assert "release_relock" not in str(step), step
    checkout = next(
        s
        for s in _steps(workflow, "reopen-dev")
        if "actions/checkout" in str(s.get("uses"))
    )
    assert checkout["with"]["ref"] == "dev"
