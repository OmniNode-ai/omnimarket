# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models the overlay home validates registry-wide documents through (OMN-22478)."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.overlay_documents.model_registry_documents import (
    ModelLaneRulesOverlay,
    ModelPillarOwnersOverlay,
    ModelRepositoryRegistryOverlay,
    ModelVocabularyOverlay,
)

pytestmark = pytest.mark.unit

ENTRY = {"name": "a", "repo": "org/a", "type": "python", "description": "d"}


def test_loads_the_way_the_overlay_home_loads_it(tmp_path: Path) -> None:
    (tmp_path / "doc").mkdir()
    (tmp_path / "doc" / "overlay.yaml").write_text(
        yaml.safe_dump({"repositories": [ENTRY]}), encoding="utf-8"
    )
    overlay = load_structured_overlay(
        "doc",
        ModelRepositoryRegistryOverlay,
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
    )
    assert overlay.repositories[0].repo == "org/a"


@pytest.mark.parametrize(
    "document",
    [
        {"repositories": []},
        {"repositories": [ENTRY, ENTRY]},
        {"repositories": [{**ENTRY, "stray": 1}]},
        {"repositories": [{**ENTRY, "repo": "no-owner"}]},
    ],
)
def test_a_wrong_registry_refuses(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelRepositoryRegistryOverlay.model_validate(document)


def test_lane_rules_refuse_an_empty_block() -> None:
    assert ModelLaneRulesOverlay(schema_version=1, rules_block="x").rules_block == "x"
    with pytest.raises(ValidationError):
        ModelLaneRulesOverlay(schema_version=1, rules_block="")


def test_pillar_owners_default_to_assignable_and_close_their_keys() -> None:
    overlay = ModelPillarOwnersOverlay.model_validate(
        {"pillars": {"p": {"owner_linear_user_id": "u"}}}
    )
    assert overlay.pillars["p"].assignable_by_lanes is True
    with pytest.raises(ValidationError):
        ModelPillarOwnersOverlay.model_validate(
            {"pillars": {"p": {"owner_linear_user_id": "u", "stray": 1}}}
        )


def test_vocabulary_accepts_lists_and_stamps() -> None:
    overlay = ModelVocabularyOverlay.model_validate(
        {
            "schema_version": 1,
            "names": ["a"],
            "names_resolved_at": datetime(2026, 1, 1, tzinfo=UTC),
            "cache_max_age_days": 30,
        }
    )
    assert overlay.model_extra is not None
    assert overlay.model_extra["names"] == ["a"]


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 1},
        {"schema_version": 1, "resolved_at": datetime(2026, 1, 1, tzinfo=UTC)},
        {"schema_version": 1, "names": [1]},
        {"schema_version": 1, "names": ["a"], "stray": "text"},
        {"schema_version": 2, "names": ["a"]},
    ],
)
def test_a_wrong_vocabulary_refuses(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelVocabularyOverlay.model_validate(document)


def test_imports_with_pydantic_and_yaml_only() -> None:
    """The overlay home's check installs pydantic and pyyaml and nothing else."""
    code = (
        "import sys\n"
        "import omnimarket.models.overlay_documents.model_registry_documents\n"
        "heavy = {'omnibase_core', 'omnibase_infra', 'omnibase_spi'} & set(sys.modules)\n"
        "assert not heavy, sorted(heavy)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
