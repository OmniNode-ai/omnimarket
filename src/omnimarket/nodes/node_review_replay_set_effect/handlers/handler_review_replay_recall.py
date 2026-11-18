# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Freeze the recall bar and score a roster against it over the replay set (OMN-22971).

The bar (minimum recall) and the labels (which frozen items carry a defect) are
written once, beside the frozen replay set, before any roster is scored; the
bar's hash is the SHA-256 of its canonical JSON, which names the replay set's
manifest hash. Scoring reads the frozen bar and the frozen set from the store,
never from the request, and refuses when either is missing or no longer matches
its hash. A defective item counts as caught when at least one roster member
blocked it; a passed, degraded or missing answer is a miss. Nothing is passed on
silence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from omnimarket.nodes.node_review_replay_set_effect.handlers.handler_review_replay_set import (
    MANIFEST_FILE,
    manifest_hash,
)
from omnimarket.nodes.node_review_replay_set_effect.models import (
    ModelReplayManifest,
)
from omnimarket.nodes.node_review_replay_set_effect.models.model_review_replay_recall import (
    EnumRosterVerdict,
    ModelFreezeRecallBarRequest,
    ModelFreezeRecallBarResult,
    ModelRecallBar,
    ModelReviewReplayRecallRequest,
    ModelReviewReplayRecallResult,
)

BAR_PREFIX = "recall_bar."
BAR_SUFFIX = ".json"
_EPSILON = 1e-12


def bar_json(bar: ModelRecallBar) -> str:
    return json.dumps(bar.model_dump(mode="json"), sort_keys=True, indent=1) + "\n"


def bar_hash(bar: ModelRecallBar) -> str:
    canonical = json.dumps(
        bar.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_frozen_set(store_root: str, digest: str) -> ModelReplayManifest:
    """Read the frozen replay set and prove it still matches its hash and its diffs."""
    directory = Path(store_root) / digest
    path = directory / MANIFEST_FILE
    if not path.exists():
        raise ValueError(f"no frozen replay set {digest} under {store_root}")
    manifest = ModelReplayManifest.model_validate_json(path.read_text(encoding="utf-8"))
    if manifest_hash(manifest) != digest:
        raise ValueError(
            f"frozen manifest at {path} no longer matches its hash {digest}"
        )
    for item in manifest.items:
        body = (directory / item.diff_file).read_bytes()
        if hashlib.sha256(body).hexdigest() != item.diff_sha256:
            raise ValueError(f"frozen diff {item.diff_file} no longer matches its hash")
    return manifest


def bar_file_name(digest: str) -> str:
    return f"{BAR_PREFIX}{digest}{BAR_SUFFIX}"


def _frozen_bar_paths(directory: Path) -> list[Path]:
    return sorted(directory.glob(f"{BAR_PREFIX}*{BAR_SUFFIX}"))


def load_frozen_bar(store_root: str, digest: str) -> tuple[ModelRecallBar, str]:
    """Read the frozen bar beside its replay set.

    The bar's file is named by its hash, so an edit after freezing no longer
    matches its own name; the hash is recomputed, never trusted.
    """
    paths = _frozen_bar_paths(Path(store_root) / digest)
    if not paths:
        raise ValueError(
            f"no recall bar frozen for replay set {digest}: freeze it before the first run"
        )
    if len(paths) > 1:
        raise ValueError(f"more than one recall bar frozen for replay set {digest}")
    path = paths[0]
    text = path.read_text(encoding="utf-8")
    bar = ModelRecallBar.model_validate_json(text)
    recomputed = bar_hash(bar)
    if bar.manifest_hash != digest:
        raise ValueError(f"recall bar at {path} names replay set {bar.manifest_hash}")
    if path.name != bar_file_name(recomputed) or text != bar_json(bar):
        raise ValueError(f"frozen recall bar at {path} was changed after it was frozen")
    return bar, recomputed


class HandlerReviewReplayFreezeBar:
    """Effect: freeze the recall bar and labels beside the replay set, once."""

    def handle(
        self, request: ModelFreezeRecallBarRequest
    ) -> ModelFreezeRecallBarResult:
        manifest = load_frozen_set(request.store_root, request.bar.manifest_hash)
        known = {item.item_id for item in manifest.items}
        unknown = sorted({label.item_id for label in request.bar.labels} - known)
        if unknown:
            raise ValueError(
                f"labels name items not in the frozen set: {', '.join(unknown)}"
            )
        digest = bar_hash(request.bar)
        directory = Path(request.store_root) / request.bar.manifest_hash
        path = directory / bar_file_name(digest)
        already = False
        if _frozen_bar_paths(directory):
            _, recorded_hash = load_frozen_bar(
                request.store_root, request.bar.manifest_hash
            )
            if recorded_hash != digest:
                raise ValueError(
                    f"a different recall bar ({recorded_hash}) is already frozen "
                    f"for replay set {request.bar.manifest_hash}"
                )
            already = True
        else:
            path.write_text(bar_json(request.bar), encoding="utf-8")
        return ModelFreezeRecallBarResult(
            correlation_id=request.correlation_id,
            manifest_hash=request.bar.manifest_hash,
            bar_hash=digest,
            bar_path=str(path),
            already_frozen=already,
        )


class HandlerReviewReplayRecall:
    """Compute: recall of a roster over the frozen replay set against the frozen bar."""

    def handle(
        self, request: ModelReviewReplayRecallRequest
    ) -> ModelReviewReplayRecallResult:
        manifest = load_frozen_set(request.store_root, request.manifest_hash)
        bar, digest = load_frozen_bar(request.store_root, request.manifest_hash)
        if (
            request.expected_bar_hash is not None
            and request.expected_bar_hash != digest
        ):
            raise ValueError(
                f"frozen recall bar is {digest}, the request expects {request.expected_bar_hash}"
            )
        known = {item.item_id for item in manifest.items}

        seen: set[tuple[str, str]] = set()
        blocked_by: dict[str, set[str]] = {}
        for obs in request.observations:
            if obs.item_id not in known:
                raise ValueError(
                    f"observation for {obs.item_id}, not in the replay set"
                )
            key = (obs.item_id, obs.member)
            if key in seen:
                raise ValueError(f"{obs.member} answered {obs.item_id} twice")
            seen.add(key)
            if obs.verdict is EnumRosterVerdict.BLOCKED:
                blocked_by.setdefault(obs.item_id, set()).add(obs.member)

        defective = sorted(label.item_id for label in bar.labels if label.defective)
        clean = [label.item_id for label in bar.labels if not label.defective]
        missed = tuple(i for i in defective if i not in blocked_by)
        caught = len(defective) - len(missed)
        recall = caught / len(defective)
        return ModelReviewReplayRecallResult(
            correlation_id=request.correlation_id,
            roster_ref=request.roster_ref,
            manifest_hash=request.manifest_hash,
            bar_hash=digest,
            min_recall=bar.min_recall,
            defective_items=len(defective),
            caught=caught,
            recall=recall,
            passed=recall + _EPSILON >= bar.min_recall,
            missed=missed,
            false_blocks=sum(1 for i in clean if i in blocked_by),
        )


__all__ = [
    "BAR_PREFIX",
    "HandlerReviewReplayFreezeBar",
    "HandlerReviewReplayRecall",
    "bar_file_name",
    "bar_hash",
    "bar_json",
    "load_frozen_bar",
    "load_frozen_set",
]
