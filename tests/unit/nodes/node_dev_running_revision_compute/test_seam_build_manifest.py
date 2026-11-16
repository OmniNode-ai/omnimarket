# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the typed build manifest the deploy agent will publish, and the facts the effect hands the compute.

The deploy agent (omnibase_infra) cannot import omnimarket and a cross-repository check that skips when the other clone is
absent is refused by the skip ratchet, so the wire fields are pinned here. The agent does not publish the manifest yet;
when it does, its publisher is a caller of ``ModelBuildManifest`` and this pin changes in the same change. This test goes
red when OUR side changes shape: a renamed, added or dropped field of the manifest, of the running build, or of the facts.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from omnimarket.models.running_revision import (
    ModelBranchInstall,
    ModelBuildManifest,
    ModelBuildRevision,
    ModelRepositoryMergeHistory,
    ModelRunningBuild,
    ModelRunningRevisionFacts,
)
from tests.unit.nodes.node_dev_running_revision_compute.conftest import build

pytestmark = pytest.mark.unit


def _fields(model: type[BaseModel]) -> tuple[str, ...]:
    return tuple(sorted(model.model_fields))


def test_the_build_manifest_fields_are_pinned() -> None:
    assert _fields(ModelBuildManifest) == (
        "branch_installs",
        "build_id",
        "built_at",
        "revisions",
        "source_hash",
    )
    assert _fields(ModelBuildRevision) == ("repository", "sha")
    assert _fields(ModelBranchInstall) == ("branch", "repository", "sha")


def test_the_running_build_and_the_facts_the_effect_hands_over_are_pinned() -> None:
    assert _fields(ModelRunningBuild) == ("manifest", "running_since")
    assert _fields(ModelRunningRevisionFacts) == (
        "builds",
        "correlation_id",
        "histories",
        "observed_at",
    )
    assert _fields(ModelRepositoryMergeHistory) == (
        "base_sha",
        "merges",
        "repository",
        "tip_sha",
    )


def test_a_manifest_survives_the_wire_unchanged() -> None:
    manifest = build("b1", 60, {"omnimarket": 1}).manifest
    again = ModelBuildManifest.model_validate_json(manifest.model_dump_json())
    assert again == manifest
