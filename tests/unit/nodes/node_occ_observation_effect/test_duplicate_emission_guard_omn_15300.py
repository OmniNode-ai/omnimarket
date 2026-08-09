# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15300: two runs observing the same head sha open ONE PR, not two.

The observe workflow fires per ``pull_request`` event, and ``edited`` is in its
trigger list, so a single head sha can produce several runs seconds apart.
omnimarket#1922 produced three runs for head sha ``cac88675`` in 32 seconds and
three OCC PRs (#5245/#5246/#5247) carrying byte-identical observations.

Each attempt gets its own path and branch by design — the store is one file per
raw attempt — so the pre-existing exact-branch lookup could never see a sibling.
The guard keys on the identity prefix the siblings share instead.

Hermetic: every GitHub call is stubbed, git is a real local repo, no network.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import ModelOccObservationRecord
from omnimarket.events.occ_observation_store import occ_observation_record_relpath
from omnimarket.nodes.node_occ_observation_effect.handlers.handler_occ_observation_effect import (
    HandlerOccObservationEffect,
    _branch_name,
    _identity_branch_prefix,
)
from omnimarket.nodes.node_occ_observation_effect.models.model_occ_observation_effect_request import (
    ModelOccObservationEffectRequest,
)
from tests.unit.nodes.node_occ_observation_effect.conftest import OCC_FIXTURE_ROOT

_HANDLER_MODULE = (
    "omnimarket.nodes.node_occ_observation_effect.handlers."
    "handler_occ_observation_effect"
)
HEAD_SHA = "cac88675d0419cb9ac4e102270dc9c43954ab613"


def _record(workflow_run_id: int) -> ModelOccObservationRecord:
    """Same observation, different workflow run — the duplicate-emission shape."""
    return ModelOccObservationRecord(
        product_repo="OmniNode-ai/omnimarket",
        product_pr_number=1922,
        head_sha=HEAD_SHA,
        policy_version="v1",
        workflow_run_id=workflow_run_id,
        run_attempt=1,
        recorded_at="2026-07-28T04:22:45Z",
        observation=ModelOccAutoauthorObservation(
            product_repo="OmniNode-ai/omnimarket",
            product_pr_number=1922,
            occ_pr_number=5161,
            minted_by_node=True,
            attestation_match=False,
            occ_preflight_eligible=True,
            observed_at="2026-07-28T04:22:38+00:00",
            reason="mismatch",
        ),
    )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _seed_repo(tmp_path: Path) -> Path:
    """Seeded from the shared OCC fixture (see conftest).

    OMN-15323: the clone must carry contracts/ + drift/dod_receipts/, because
    the write path now authors self-bind evidence into them. An empty repo is a
    shape onex_change_control never has.
    """
    seed = tmp_path / "seed"
    shutil.copytree(OCC_FIXTURE_ROOT, seed)
    _git(seed, "init", "-q")
    _git(seed, "config", "user.name", "test")
    _git(seed, "config", "user.email", "test@omninode.ai")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "OCC fixture base")
    return seed


class FakeGitHub:
    """Minimal stand-in for the REST calls the handler makes."""

    def __init__(self) -> None:
        self.open_prs: list[dict[str, Any]] = []
        self.created_titles: list[str] = []
        self.created_bodies: list[str] = []
        self.commit_probes: list[str] = []
        self._next_number = 5245

    def rest_json_array(
        self, _method: str, path: str, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        if "head=" in path:
            wanted = path.split("head=")[1].split("&")[0].split(":", 1)[1]
            return [pr for pr in self.open_prs if pr["head"]["ref"] == wanted]
        return list(reversed(self.open_prs))

    def rest_json(
        self,
        _method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        # OMN-15323: the self-bind receipt's probe reads the pushed record
        # commit back from the remote before claiming PASS.
        if body is None:
            sha = path.rsplit("/", 1)[-1]
            self.commit_probes.append(sha)
            return {"sha": sha}
        number = self._next_number
        self._next_number += 1
        url = f"https://github.com/OmniNode-ai/onex_change_control/pull/{number}"
        self.open_prs.append(
            {
                "number": number,
                "html_url": url,
                # OMN-15777: the selector now requires the head branch to be
                # IN the OCC repo itself (fork-repo hardening), so the fake
                # must carry that field for a match to succeed.
                "head": {
                    "ref": body["head"],
                    "repo": {"full_name": "OmniNode-ai/onex_change_control"},
                },
            }
        )
        self.created_titles.append(body["title"])
        self.created_bodies.append(body["body"])
        return {"number": number, "html_url": url}


def _install(
    handler: HandlerOccObservationEffect,
    fake: FakeGitHub,
    seed: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f"{_HANDLER_MODULE}._resolve_github_token", lambda: "tok")
    monkeypatch.setattr(f"{_HANDLER_MODULE}.rest_json_array", fake.rest_json_array)
    monkeypatch.setattr(f"{_HANDLER_MODULE}.rest_json", fake.rest_json)
    monkeypatch.setattr(
        handler,
        "_clone_default",
        lambda clone_dir, _t, _r: (shutil.copytree(seed, clone_dir), "dev")[1],
    )
    # OMN-15777: the reuse path clones directly onto an existing PR's branch
    # instead of the default branch. This fake doesn't model a real evolving
    # remote (that is covered by test_reuse_open_observation_pr_omn_15777.py's
    # real-bare-origin fixture) — it only needs to prove WHICH branch/PR this
    # run targets, so cloning the same static seed is sufficient here too.
    monkeypatch.setattr(
        handler,
        "_clone_branch",
        lambda clone_dir, _t, _r, _b: shutil.copytree(seed, clone_dir),
    )
    monkeypatch.setattr(handler, "_push", lambda *_a, **_k: None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_identity_second_run_opens_no_second_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = HandlerOccObservationEffect()
    fake = FakeGitHub()
    _install(handler, fake, _seed_repo(tmp_path), monkeypatch)

    first = await handler.handle(
        ModelOccObservationEffectRequest(record=_record(30328534324), mode="mutate")
    )
    second = await handler.handle(
        ModelOccObservationEffectRequest(record=_record(30328537148), mode="mutate")
    )

    assert first.superseded_by_open_pr is False
    assert first.occ_pr_number == 5245

    assert second.superseded_by_open_pr is True
    assert second.occ_pr_number == 5245, "must reuse the sibling's PR, not open one"
    assert len(fake.created_titles) == 1, "exactly one PR-open attempt for one identity"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_different_head_sha_appends_to_the_open_pr_not_a_second_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not swallow a genuinely new observation (OMN-15300) —
    but OMN-15777 widened the DESTINATION: a different identity is still
    WRITTEN, just onto the one already-open observation PR rather than a
    fresh one (this is what kills the cross-identity conflict factory)."""
    handler = HandlerOccObservationEffect()
    fake = FakeGitHub()
    _install(handler, fake, _seed_repo(tmp_path), monkeypatch)

    await handler.handle(
        ModelOccObservationEffectRequest(record=_record(30328534324), mode="mutate")
    )
    other = _record(30328560931).model_copy(
        update={"head_sha": "0000000000000000000000000000000000000000"}
    )
    second = await handler.handle(
        ModelOccObservationEffectRequest(record=other, mode="mutate")
    )

    assert second.superseded_by_open_pr is False, (
        "a genuinely new observation is written"
    )
    assert second.appended_to_existing_pr is True
    assert second.occ_pr_number == 5245, "reuses the sibling's PR, no second PR opens"
    assert len(fake.created_titles) == 1, "still exactly one PR-open attempt"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_created_pr_carries_the_bindable_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end at the write seam: the PR the handler actually opens binds."""
    handler = HandlerOccObservationEffect()
    fake = FakeGitHub()
    _install(handler, fake, _seed_repo(tmp_path), monkeypatch)

    await handler.handle(
        ModelOccObservationEffectRequest(record=_record(30328534324), mode="mutate")
    )

    from omnibase_core.validation.validator_receipt_gate import _extract_ticket_ids

    assert _extract_ticket_ids(fake.created_bodies[0], fake.created_titles[0]) == [
        "OMN-14888"
    ]
    assert fake.created_titles[0].startswith("evidence(OMN-14888):")
    assert "Evidence-Ticket: OMN-14888" in fake.created_bodies[0]


class TestIdentityPrefix:
    def test_prefix_is_shared_by_attempts_and_is_a_real_prefix(self) -> None:
        a = occ_observation_record_relpath(_record(30328534324))
        b = occ_observation_record_relpath(_record(30328537148))
        assert _identity_branch_prefix(a) == _identity_branch_prefix(b)
        assert _branch_name(a).startswith(_identity_branch_prefix(a))
        assert _branch_name(a) != _branch_name(b)

    def test_prefix_differs_across_head_shas(self) -> None:
        a = occ_observation_record_relpath(_record(1))
        other = _record(1).model_copy(update={"head_sha": "b" * 40})
        assert _identity_branch_prefix(a) != _identity_branch_prefix(
            occ_observation_record_relpath(other)
        )

    def test_missing_run_marker_degrades_to_exact_branch(self) -> None:
        """Fail-safe: an unexpected path must not over-match unrelated PRs."""
        odd = "drift/occ_observations/x/pr-1/no-marker.yaml"
        assert _identity_branch_prefix(odd) == _branch_name(odd)
