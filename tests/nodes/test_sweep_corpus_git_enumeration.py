# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: the planted canary below IS the pattern the sweep under test
# detects; matching the sibling test_aislop_hardcoded_paths.py convention.
"""Corpus-enumeration tests for the sweep shelf (OMN-18472).

The three tree-walking sweeps — aislop, compliance, contract — used to build
their scan corpus with ``Path.rglob`` over the whole clone. On ``omnibase_infra``
that walk visited 23,245 ``*.py`` files, 17,288 of them under ``workspace/``:
gitignored copies of OTHER repositories staged for docker builds. Compliance
reported 155 violations against ``omnibase_infra`` for code owned by
``omnibase_core``, and contract_sweep reported ``node_contract_drift_orchestrator``
— a node that exists only in ``onex_change_control``.

These tests pin the replacement corpus and, just as importantly, its BOUNDARIES:

* tracked files are scanned (the corpus did not narrow to nothing);
* untracked-but-not-ignored files are scanned, so a sweep running in pre-commit
  still sees a NEW file the author has not yet ``git add``ed;
* gitignored files are NOT scanned;
* a root outside a git working tree raises rather than falling back to a walk.

The last one is the load-bearing negative. A filesystem fallback would make a
correct run and a run over the wrong corpus produce the same shaped verdict,
which is the silent-narrowing failure the sweep shelf exists to catch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
    AislopSweepRequest,
    NodeAislopSweep,
)
from omnimarket.nodes.sweep_scope import (
    SweepCorpusUnresolvedError,
    collect_git_corpus,
)
from tests.sweep_corpus_fixture import init_fixture_repo

# A line that trips the aislop ``hardcoded-paths`` check (CLAUDE.md rule 6).
PLANTED_VIOLATION = 'BAD_PATH = "/Users/someone/Code/thing"\n'


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A real git repository carrying one file of each corpus class.

    ``tracked.py`` is committed; ``untracked.py`` is written but never added;
    ``.venv/ignored.py`` and ``src/staged_copy/vendored.py`` are written under
    gitignored paths. All four contain the same planted violation, so any
    difference in findings is attributable to the enumeration and nothing else.
    """
    repo = tmp_path / "fixture_repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".venv").mkdir()
    # Gitignored but NOT in any handler's ``_EXCLUDED_DIRS``. This is the
    # directory that makes the handler-level boundary test real: ``.venv``
    # alone is already excluded by name, so a corpus regression to a
    # filesystem walk would still pass a .venv-only assertion.
    (repo / "src" / "staged_copy").mkdir()

    (repo / ".gitignore").write_text(".venv/\nsrc/staged_copy/\n")
    (repo / "src" / "tracked.py").write_text(PLANTED_VIOLATION)
    (repo / "src" / "untracked.py").write_text(PLANTED_VIOLATION)
    (repo / ".venv" / "ignored.py").write_text(PLANTED_VIOLATION)
    (repo / "src" / "staged_copy" / "vendored.py").write_text(PLANTED_VIOLATION)

    # Only .gitignore and tracked.py are committed; untracked.py is left
    # untracked-but-not-ignored, which the corpus must still include.
    init_fixture_repo(repo, track=[".gitignore", "src/tracked.py"])
    return repo


@pytest.mark.unit
def test_corpus_includes_tracked_files(fixture_repo: Path) -> None:
    """Positive control: the corpus is not empty and contains committed files."""
    names = {p.name for p in collect_git_corpus(fixture_repo, "*.py")}
    assert "tracked.py" in names


@pytest.mark.unit
def test_corpus_includes_untracked_not_ignored_files(fixture_repo: Path) -> None:
    """AC3: a new file the author has not ``git add``ed is still scanned.

    Tracked-only enumeration would report clean over exactly the code most
    likely to carry a fresh violation, which is why the corpus unions
    ``ls-files`` with ``ls-files --others --exclude-standard``.
    """
    names = {p.name for p in collect_git_corpus(fixture_repo, "*.py")}
    assert "untracked.py" in names


@pytest.mark.unit
def test_corpus_excludes_gitignored_files(fixture_repo: Path) -> None:
    """AC4: an ignored path is not scanned, even carrying the same violation."""
    paths = collect_git_corpus(fixture_repo, "*.py")
    assert not [p for p in paths if ".venv" in p.parts]
    assert not [p for p in paths if "staged_copy" in p.parts]


@pytest.mark.unit
def test_corpus_matches_exact_filename_pattern(fixture_repo: Path) -> None:
    """``contract.yaml`` must match by whole name, not as a suffix.

    ``node_contract.yaml`` is a different file; matching it would silently widen
    the contract sweep's corpus.
    """
    (fixture_repo / "src" / "contract.yaml").write_text("x: 1\n")
    (fixture_repo / "src" / "node_contract.yaml").write_text("x: 1\n")
    names = {p.name for p in collect_git_corpus(fixture_repo, "contract.yaml")}
    assert names == {"contract.yaml"}


@pytest.mark.unit
def test_corpus_drops_tracked_file_deleted_from_worktree(fixture_repo: Path) -> None:
    """A tracked path with no file on disk is not corpus — a sweep reads bytes."""
    (fixture_repo / "src" / "tracked.py").unlink()
    names = {p.name for p in collect_git_corpus(fixture_repo, "*.py")}
    assert "tracked.py" not in names


@pytest.mark.unit
def test_root_outside_git_repo_raises(tmp_path: Path) -> None:
    """AC7: no filesystem fallback — a non-repo root is a hard error.

    The error must name the path, and nothing may be returned: a fallback walk
    here is how a sweep scans a different corpus under the same verdict.
    """
    outside = tmp_path / "not_a_repo"
    (outside / "src").mkdir(parents=True)
    (outside / "src" / "thing.py").write_text(PLANTED_VIOLATION)

    with pytest.raises(SweepCorpusUnresolvedError) as excinfo:
        collect_git_corpus(outside, "*.py")
    assert str(outside) in str(excinfo.value)


@pytest.mark.unit
def test_missing_root_raises(tmp_path: Path) -> None:
    """A scan root that does not exist errors rather than returning nothing."""
    with pytest.raises(SweepCorpusUnresolvedError):
        collect_git_corpus(tmp_path / "absent", "*.py")


@pytest.mark.unit
def test_enumeration_source_is_git_ls_files(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1/AC6: pin the enumeration source so a revert to rglob fails here.

    Asserts BOTH git enumerations are issued. A future edit that keeps only
    ``ls-files`` would quietly drop untracked-not-ignored files and still pass
    every other test in this module that uses a committed fixture.
    """
    calls: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("omnimarket.nodes.sweep_scope.subprocess.run", recording_run)
    collect_git_corpus(fixture_repo, "*.py")

    ls_files = [c for c in calls if "ls-files" in c]
    assert len(ls_files) == 2, f"expected two git ls-files calls, got {calls}"
    assert any("--others" in c and "--exclude-standard" in c for c in ls_files)
    assert any("--others" not in c for c in ls_files)


@pytest.mark.unit
def test_git_pointer_env_is_scrubbed(
    fixture_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inherited ``GIT_DIR`` must not redirect the enumeration.

    A sweep invoked from a pre-commit hook inherits the hook's ``GIT_DIR``. If
    that reached git, the corpus would come from the hook's repository while
    the verdict was reported against the requested root (OMN-18434 class).
    """
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "some" / "other" / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "some" / "other"))

    names = {p.name for p in collect_git_corpus(fixture_repo, "*.py")}
    assert "tracked.py" in names


@pytest.mark.unit
def test_aislop_handler_respects_corpus_boundaries(fixture_repo: Path) -> None:
    """End-to-end over the real handler: AC2 positive control plus AC3 and AC4.

    The same violation is planted in a tracked file, an untracked-not-ignored
    file and two ignored files. The first two must be found — that is the
    positive control proving a zero here would mean something — and neither
    ignored file may be.

    ``src/staged_copy/`` is the load-bearing one. It is gitignored but absent
    from the handler's ``_EXCLUDED_DIRS``, so it stands in for the real
    ``workspace/sibling-repos/`` trees that made this sweep report 155
    ``omnibase_core`` violations against ``omnibase_infra``. A regression to a
    filesystem walk is invisible to a ``.venv``-only assertion.
    """
    result = NodeAislopSweep(event_bus=None).handle(
        AislopSweepRequest(target_dirs=[str(fixture_repo)], checks=["hardcoded-paths"])
    )
    found = {f.path for f in result.findings}

    assert "src/tracked.py" in found, "tracked file not scanned — corpus is broken"
    assert "src/untracked.py" in found, "untracked-not-ignored file was dropped"
    assert not [p for p in found if ".venv" in p], f"ignored path scanned: {found}"
    assert not [p for p in found if "staged_copy" in p], (
        f"gitignored staged copy scanned: {found}"
    )
