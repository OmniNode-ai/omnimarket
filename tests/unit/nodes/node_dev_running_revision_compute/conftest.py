# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders: the policy comes from a tmp overlay, as the node reads it in production."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_dev_running_revision_compute.models import (
    ModelBuildManifest,
    ModelBuildRevision,
    ModelDevMerge,
    ModelRepositoryMergeHistory,
    ModelRunningBuild,
    ModelRunningRevisionFacts,
    ModelRunningRevisionPolicy,
)
from omnimarket.nodes.node_dev_running_revision_compute.overlay import (
    NODE_NAME,
    load_dev_running_revision_policy,
)

NOW = datetime(2026, 11, 12, 16, 0, tzinfo=UTC)
POLICY_DOC: dict[str, object] = {
    "known_repositories": ["omnimarket", "omnibase_infra"],
    "behind_threshold_seconds": 3600,
    "median_merge_to_running_bound_seconds": 3600,
}


def write_overlay(root: Path, doc: dict[str, object] | None = None) -> dict[str, str]:
    """Write ``<root>/<node>/overlay.yaml`` and return the environ that points at it."""
    node_dir = root / NODE_NAME
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(POLICY_DOC if doc is None else doc), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


@pytest.fixture
def policy(tmp_path: Path) -> ModelRunningRevisionPolicy:
    return load_dev_running_revision_policy(write_overlay(tmp_path))


def sha(n: int) -> str:
    """A 40 lowercase hex string built from ``n``."""
    return f"{n:040x}"


def merge(n: int, age_seconds: int) -> ModelDevMerge:
    """A merge of ``sha(n)`` merged ``age_seconds`` before NOW."""
    return ModelDevMerge(sha=sha(n), merged_at=NOW - timedelta(seconds=age_seconds))


def history(
    repository: str, base: str, merges: tuple[ModelDevMerge, ...]
) -> ModelRepositoryMergeHistory:
    """A merge history whose tip is the last merge's sha (or ``base`` when none)."""
    tip_sha = merges[-1].sha if merges else base
    return ModelRepositoryMergeHistory(
        repository=repository, base_sha=base, tip_sha=tip_sha, merges=merges
    )


def build(
    build_id: str,
    ago_seconds: int,
    revisions: dict[str, int],
    source_hash: str | None = None,
) -> ModelRunningBuild:
    """A running build whose manifest ``built_at`` is ``running_since - 60 s``."""
    running_since = NOW - timedelta(seconds=ago_seconds)
    manifest = ModelBuildManifest(
        build_id=build_id,
        built_at=running_since - timedelta(seconds=60),
        source_hash=source_hash,
        branch_installs=(),
        revisions=tuple(
            ModelBuildRevision(repository=repo, sha=sha(n))
            for repo, n in revisions.items()
        ),
    )
    return ModelRunningBuild(manifest=manifest, running_since=running_since)


def facts(
    builds: tuple[ModelRunningBuild, ...],
    histories: tuple[ModelRepositoryMergeHistory, ...],
) -> ModelRunningRevisionFacts:
    """A facts snapshot with a fresh correlation id and ``observed_at`` NOW."""
    return ModelRunningRevisionFacts(
        correlation_id=uuid4(),
        observed_at=NOW,
        builds=builds,
        histories=histories,
    )
