# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recall over the review replay set against a bar frozen before the first run (OMN-22971)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_review_replay_set_effect.handlers.handler_review_replay_recall import (
    HandlerReviewReplayFreezeBar,
    HandlerReviewReplayRecall,
    bar_file_name,
    bar_hash,
)
from omnimarket.nodes.node_review_replay_set_effect.handlers.handler_review_replay_set import (
    HandlerReviewReplaySet,
)
from omnimarket.nodes.node_review_replay_set_effect.models import (
    EnumRosterVerdict,
    ModelFreezeRecallBarRequest,
    ModelMergedCandidate,
    ModelRecallBar,
    ModelReplayLabel,
    ModelReplayRepo,
    ModelReviewReplayRecallRequest,
    ModelReviewReplayRecallResult,
    ModelReviewReplaySetRequest,
    ModelRosterObservation,
)

pytestmark = pytest.mark.unit

ITEMS = 10


class _Source:
    def list_merged(
        self, repo: ModelReplayRepo, limit: int
    ) -> list[ModelMergedCandidate]:
        return [
            ModelMergedCandidate(
                pr_number=n,
                merge_sha=f"{n:040x}",
                parent_sha=f"{n + 1000:040x}",
                title=f"change {n} (#{n})",
                changed_lines=10 * n,
            )
            for n in range(1, ITEMS + 1)
        ]

    def read_diff(self, repo: ModelReplayRepo, candidate: ModelMergedCandidate) -> str:
        return f"diff --git a/f b/f\n+{candidate.merge_sha}\n"


def _freeze_set(tmp_path: Path) -> tuple[str, list[str]]:
    result = HandlerReviewReplaySet(source=_Source()).handle(
        ModelReviewReplaySetRequest(
            correlation_id="c",
            repos=(ModelReplayRepo(repo="alpha", clone_path="/unused"),),
            store_root=str(tmp_path),
            target_count=ITEMS,
            seed="s",
        )
    )
    manifest = Path(result.manifest_path).read_text(encoding="utf-8")
    import json

    ids = [i["item_id"] for i in json.loads(manifest)["items"]]
    return result.manifest_hash, ids


def _bar(
    digest: str, ids: list[str], defective: int = 5, bar: float = 0.8
) -> ModelRecallBar:
    return ModelRecallBar(
        manifest_hash=digest,
        min_recall=bar,
        labels=tuple(
            ModelReplayLabel(
                item_id=item, defective=n < defective, evidence=f"record {n}"
            )
            for n, item in enumerate(ids)
        ),
    )


def _freeze_bar(tmp_path: Path, bar: ModelRecallBar) -> str:
    return (
        HandlerReviewReplayFreezeBar()
        .handle(
            ModelFreezeRecallBarRequest(
                correlation_id="c", store_root=str(tmp_path), bar=bar
            )
        )
        .bar_hash
    )


def _score(
    tmp_path: Path, digest: str, blocked: list[str], ids: list[str], roster: str = "r"
) -> ModelReviewReplayRecallResult:
    observations = tuple(
        ModelRosterObservation(
            item_id=item,
            member="m1",
            verdict=(
                EnumRosterVerdict.BLOCKED
                if item in blocked
                else EnumRosterVerdict.PASSED
            ),
        )
        for item in ids
    )
    return HandlerReviewReplayRecall().handle(
        ModelReviewReplayRecallRequest(
            correlation_id="c",
            store_root=str(tmp_path),
            manifest_hash=digest,
            roster_ref=roster,
            observations=observations,
        )
    )


def test_recall_bar_roster_at_the_bar_passes(tmp_path: Path) -> None:
    digest, ids = _freeze_set(tmp_path)
    _freeze_bar(tmp_path, _bar(digest, ids))
    defective = ids[:5]
    result = _score(tmp_path, digest, defective[:4], ids)
    assert result.defective_items == 5
    assert result.caught == 4
    assert result.recall == pytest.approx(0.8)
    assert result.passed is True
    assert result.missed == (defective[4],)


def test_recall_bar_roster_below_the_bar_fails(tmp_path: Path) -> None:
    digest, ids = _freeze_set(tmp_path)
    _freeze_bar(tmp_path, _bar(digest, ids))
    result = _score(tmp_path, digest, ids[:3], ids)
    assert result.recall == pytest.approx(0.6)
    assert result.passed is False
    assert len(result.missed) == 2


def test_recall_bar_degraded_and_missing_answers_are_misses(tmp_path: Path) -> None:
    digest, ids = _freeze_set(tmp_path)
    _freeze_bar(tmp_path, _bar(digest, ids, bar=0.2))
    observations = (
        ModelRosterObservation(
            item_id=ids[0], member="m1", verdict=EnumRosterVerdict.DEGRADED
        ),
    )
    result = HandlerReviewReplayRecall().handle(
        ModelReviewReplayRecallRequest(
            correlation_id="c",
            store_root=str(tmp_path),
            manifest_hash=digest,
            roster_ref="r",
            observations=observations,
        )
    )
    assert result.caught == 0
    assert result.recall == 0.0
    assert result.passed is False


def test_recall_bar_any_member_blocking_counts_and_clean_blocks_are_counted(
    tmp_path: Path,
) -> None:
    digest, ids = _freeze_set(tmp_path)
    _freeze_bar(tmp_path, _bar(digest, ids))
    observations = tuple(
        ModelRosterObservation(
            item_id=item,
            member=member,
            verdict=(
                EnumRosterVerdict.BLOCKED
                if (member == "m1" and item in ids[:3])
                or (member == "m2" and item in ids[3:6])
                else EnumRosterVerdict.PASSED
            ),
        )
        for item in ids
        for member in ("m1", "m2")
    )
    result = HandlerReviewReplayRecall().handle(
        ModelReviewReplayRecallRequest(
            correlation_id="c",
            store_root=str(tmp_path),
            manifest_hash=digest,
            roster_ref="r",
            observations=observations,
        )
    )
    assert result.caught == 5
    assert result.passed is True
    assert result.false_blocks == 1


def test_recall_bar_observation_outside_the_set_or_twice_is_refused(
    tmp_path: Path,
) -> None:
    digest, ids = _freeze_set(tmp_path)
    _freeze_bar(tmp_path, _bar(digest, ids))
    for observations in (
        (
            ModelRosterObservation(
                item_id="nope", member="m", verdict=EnumRosterVerdict.PASSED
            ),
        ),
        (
            ModelRosterObservation(
                item_id=ids[0], member="m", verdict=EnumRosterVerdict.PASSED
            ),
            ModelRosterObservation(
                item_id=ids[0], member="m", verdict=EnumRosterVerdict.BLOCKED
            ),
        ),
    ):
        with pytest.raises(ValueError, match=r"not in the replay set|twice"):
            HandlerReviewReplayRecall().handle(
                ModelReviewReplayRecallRequest(
                    correlation_id="c",
                    store_root=str(tmp_path),
                    manifest_hash=digest,
                    roster_ref="r",
                    observations=observations,
                )
            )


def test_recall_bar_set_with_no_defective_label_is_unmeasurable() -> None:
    with pytest.raises(ValueError, match="recall is undefined"):
        ModelRecallBar(
            manifest_hash="a" * 64,
            min_recall=0.5,
            labels=(ModelReplayLabel(item_id="x", defective=False, evidence="e"),),
        )


def test_frozen_hash_scoring_refuses_a_bar_that_was_never_frozen(
    tmp_path: Path,
) -> None:
    digest, ids = _freeze_set(tmp_path)
    with pytest.raises(ValueError, match="freeze it before the first run"):
        _score(tmp_path, digest, ids[:5], ids)


def test_frozen_hash_is_the_hash_of_the_bar_and_names_the_set(tmp_path: Path) -> None:
    digest, ids = _freeze_set(tmp_path)
    bar = _bar(digest, ids)
    frozen = _freeze_bar(tmp_path, bar)
    assert frozen == bar_hash(bar)
    assert len(frozen) == 64
    assert (tmp_path / digest / bar_file_name(frozen)).exists()
    result = _score(tmp_path, digest, ids[:5], ids)
    assert result.bar_hash == frozen
    assert result.manifest_hash == digest
    # the same bar freezes again as the same hash; a changed bar has another hash
    assert _freeze_bar(tmp_path, bar) == frozen
    assert bar_hash(_bar(digest, ids, bar=0.9)) != frozen


def test_frozen_hash_a_different_bar_cannot_replace_the_frozen_one(
    tmp_path: Path,
) -> None:
    digest, ids = _freeze_set(tmp_path)
    _freeze_bar(tmp_path, _bar(digest, ids))
    with pytest.raises(ValueError, match="already frozen"):
        _freeze_bar(tmp_path, _bar(digest, ids, bar=0.5))


def test_frozen_hash_a_bar_edited_after_freezing_is_refused(tmp_path: Path) -> None:
    digest, ids = _freeze_set(tmp_path)
    frozen = _freeze_bar(tmp_path, _bar(digest, ids))
    path = tmp_path / digest / bar_file_name(frozen)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '"min_recall": 0.8', '"min_recall": 0.1'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="changed after it was frozen"):
        _score(tmp_path, digest, ids[:5], ids)


def test_frozen_hash_a_changed_replay_diff_is_refused(tmp_path: Path) -> None:
    digest, ids = _freeze_set(tmp_path)
    _freeze_bar(tmp_path, _bar(digest, ids))
    next((tmp_path / digest / "diffs").glob("*.diff")).write_text("tampered\n")
    with pytest.raises(ValueError, match="no longer matches its hash"):
        _score(tmp_path, digest, ids[:5], ids)


def test_frozen_hash_labels_must_name_items_of_the_set(tmp_path: Path) -> None:
    digest, _ids = _freeze_set(tmp_path)
    bad = ModelRecallBar(
        manifest_hash=digest,
        min_recall=0.5,
        labels=(ModelReplayLabel(item_id="ghost", defective=True, evidence="e"),),
    )
    with pytest.raises(ValueError, match="not in the frozen set"):
        _freeze_bar(tmp_path, bad)


def test_frozen_hash_a_run_pinned_to_another_bar_hash_is_refused(
    tmp_path: Path,
) -> None:
    digest, ids = _freeze_set(tmp_path)
    _freeze_bar(tmp_path, _bar(digest, ids))
    with pytest.raises(ValueError, match="the request expects"):
        HandlerReviewReplayRecall().handle(
            ModelReviewReplayRecallRequest(
                correlation_id="c",
                store_root=str(tmp_path),
                manifest_hash=digest,
                roster_ref="r",
                expected_bar_hash="0" * 64,
                observations=(),
            )
        )
