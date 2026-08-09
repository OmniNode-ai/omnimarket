# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14888: re-ingesting the identical raw attempt is a no-op, never a
duplicate row, an error, or a second PR — proven at the `_write_sync` seam with
a real local git "clone" (no network) standing in for the OCC checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import ModelOccObservationRecord
from omnimarket.events.occ_observation_store import (
    occ_observation_record_relpath,
    render_occ_observation_record,
)
from omnimarket.nodes.node_occ_observation_effect.handlers.handler_occ_observation_effect import (
    HandlerOccObservationEffect,
)
from omnimarket.nodes.node_occ_observation_effect.models.model_occ_observation_effect_request import (
    ModelOccObservationEffectRequest,
)


@pytest.fixture(autouse=True)
def _clear_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(var, raising=False)


def _record() -> ModelOccObservationRecord:
    return ModelOccObservationRecord(
        product_repo="OmniNode-ai/omnimarket",
        product_pr_number=1841,
        head_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        policy_version="v1",
        workflow_run_id=100,
        run_attempt=1,
        recorded_at="2026-07-21T00:00:00Z",
        observation=ModelOccAutoauthorObservation(
            product_repo="OmniNode-ai/omnimarket",
            product_pr_number=1841,
            occ_pr_number=4500,
            minted_by_node=True,
            attestation_match=True,
            occ_preflight_eligible=True,
            observed_at="2026-07-21T00:00:00Z",
            reason="",
        ),
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reingesting_the_same_attempt_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()
    relpath = occ_observation_record_relpath(record)

    # A local "clone" that already carries this exact attempt's file — as if
    # a prior run already appended it.
    clone_dir = tmp_path / "onex_change_control"
    clone_dir.mkdir()
    _git(clone_dir, "init", "-q")
    _git(clone_dir, "config", "user.name", "test")
    _git(clone_dir, "config", "user.email", "test@omninode.ai")
    existing = clone_dir / relpath
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(render_occ_observation_record(record), encoding="utf-8")
    _git(clone_dir, "add", "-A")
    _git(clone_dir, "commit", "-q", "-m", "pre-existing attempt")

    handler = HandlerOccObservationEffect()
    monkeypatch.setattr(
        "omnimarket.nodes.node_occ_observation_effect.handlers.handler_occ_observation_effect._resolve_github_token",
        lambda: "dummy-token",
    )
    # OMN-15300: the write path now probes for an open sibling PR before
    # cloning. No sibling exists in this scenario.
    monkeypatch.setattr(handler, "_open_pr_for_identity", lambda *_a, **_k: None)
    # OMN-15777: the write path also probes for ANY open observation PR
    # (cross-identity reuse target) before deciding which branch to clone.
    # None exists in this scenario.
    monkeypatch.setattr(
        handler, "_find_reusable_observation_pr", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        handler,
        "_clone_default",
        lambda clone_dir_arg, _token, _occ_repo: (
            _copy_into(clone_dir, clone_dir_arg) or "dev"
        ),
    )

    def _boom_push(*args: object, **kwargs: object) -> None:
        raise AssertionError("no-op re-ingestion must never push")

    def _boom_pr(*args: object, **kwargs: object) -> tuple[int, str]:
        raise AssertionError("no-op re-ingestion must never open/sync a PR")

    monkeypatch.setattr(handler, "_push", _boom_push)
    monkeypatch.setattr(handler, "_open_or_sync_occ_pr", _boom_pr)

    request = ModelOccObservationEffectRequest(record=record, mode="mutate")
    result = await handler.handle(request)

    assert result.already_present is True
    assert result.occ_pr_number is None
    assert relpath in result.action


def _copy_into(source: Path, dest: str) -> None:
    import shutil

    shutil.copytree(source, dest)
