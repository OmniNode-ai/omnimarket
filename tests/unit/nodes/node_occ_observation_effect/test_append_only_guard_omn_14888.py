# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14888: F-01 append-only guard parity for node_occ_observation_effect.

Ported from ``test_append_only_guard_omn_14785.py`` (node_occ_companion_effect)
so the observation write-EFFECT gets the SAME real-git-diff guard before any
live wiring — a generated tree that deletes or edits anything outside this
run's single net-new record path is rejected, never silently pushed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_occ_observation_effect.handlers.handler_occ_observation_effect import (
    HandlerOccObservationEffect,
)


@pytest.fixture(autouse=True)
def _clear_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear inherited GIT_* vars so git ops target the temp repo, not the real
    worktree (reference_git_env_vars_override_c_and_cwd)."""
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(var, raising=False)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_repo_with_existing_record(tmp_path: Path) -> str:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "config", "user.email", "test@omninode.ai")
    existing = (
        tmp_path
        / "drift"
        / "occ_observations"
        / "OmniNode-ai__omnimarket"
        / "pr-1"
        / "existing.yaml"
    )
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("head_sha: existing\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base existing observation record")
    return _git(tmp_path, "rev-parse", "HEAD")


def _add_and_commit(tmp_path: Path, rel: str, content: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", f"add {rel}")


_NEW_RECORD_PATH = "drift/occ_observations/OmniNode-ai__omnimarket/pr-1/new.yaml"


@pytest.mark.unit
class TestAppendOnlyGuard:
    def test_pure_add_of_the_one_allowed_record_passes(self, tmp_path: Path) -> None:
        base = _init_repo_with_existing_record(tmp_path)
        _add_and_commit(tmp_path, _NEW_RECORD_PATH, "head_sha: new\n")
        HandlerOccObservationEffect()._assert_append_only(
            str(tmp_path), base, {_NEW_RECORD_PATH}
        )

    def test_add_outside_the_allowed_path_is_rejected(self, tmp_path: Path) -> None:
        base = _init_repo_with_existing_record(tmp_path)
        _add_and_commit(tmp_path, _NEW_RECORD_PATH, "head_sha: new\n")
        _add_and_commit(tmp_path, "unexpected/foreign.yaml", "not: mine\n")
        with pytest.raises(RuntimeError, match="append-only violation"):
            HandlerOccObservationEffect()._assert_append_only(
                str(tmp_path), base, {_NEW_RECORD_PATH}
            )

    def test_deleting_an_existing_record_is_rejected(self, tmp_path: Path) -> None:
        base = _init_repo_with_existing_record(tmp_path)
        existing_rel = (
            "drift/occ_observations/OmniNode-ai__omnimarket/pr-1/existing.yaml"
        )
        (tmp_path / existing_rel).unlink()
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "delete existing record")
        with pytest.raises(RuntimeError, match="deletes"):
            HandlerOccObservationEffect()._assert_append_only(
                str(tmp_path), base, {_NEW_RECORD_PATH}
            )

    def test_modifying_an_existing_record_is_rejected(self, tmp_path: Path) -> None:
        """The append-only invariant this whole node exists to enforce: an
        already-durable observation row is never rewritten, matching the OCC
        companion producer's OCC#4293/4295/4296 failure mode."""
        base = _init_repo_with_existing_record(tmp_path)
        existing_rel = (
            "drift/occ_observations/OmniNode-ai__omnimarket/pr-1/existing.yaml"
        )
        (tmp_path / existing_rel).write_text("head_sha: mutated\n", encoding="utf-8")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "mutate existing record")
        with pytest.raises(RuntimeError, match="append-only violation"):
            HandlerOccObservationEffect()._assert_append_only(
                str(tmp_path), base, {_NEW_RECORD_PATH}
            )
