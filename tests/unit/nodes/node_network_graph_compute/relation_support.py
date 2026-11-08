# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixture builders for the import relation tests: edges, the shipped example overlay, a clock."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.import_relations import (
    ModelImportEdge,
    ModelImportRelationOverlay,
    ModelImportRelationRequest,
    ModelLabelOwner,
    ModelRelationKey,
)

REFERENCE_TIME = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
EXAMPLE_OVERLAY = (
    Path(__file__).resolve().parents[3].parent
    / "src/omnimarket/nodes/node_network_graph_compute/relation.overlay.example.yaml"
)


def example_overlay() -> ModelImportRelationOverlay:
    """The overlay the node ships, read as the caller would read it."""
    return ModelImportRelationOverlay.model_validate(
        yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8"))
    )


def bare_overlay(
    *tracked: tuple[str, str], owners: dict[str, int] | None = None
) -> ModelImportRelationOverlay:
    """No label rules (a repository is its own label), the example joins, chosen relations."""
    base = example_overlay()
    return base.model_copy(
        update={
            "label_rules": (),
            "tracked_relations": tuple(
                ModelRelationKey(source=s, target=t) for s, t in tracked
            ),
            "label_owners": tuple(
                ModelLabelOwner(label=k, piece=v) for k, v in (owners or {}).items()
            ),
        }
    )


def edge(importer: str, imported: str) -> ModelImportEdge:
    """``repo:path`` imports ``repo:path``."""
    importer_repo, importer_path = importer.split(":", 1)
    imported_repo, imported_path = imported.split(":", 1)
    return ModelImportEdge(
        importer_repo=importer_repo,
        importer_path=importer_path,
        imported_repo=imported_repo,
        imported_path=imported_path,
    )


def request(
    edges: tuple[ModelImportEdge, ...],
    overlay: ModelImportRelationOverlay,
    *,
    keep_stronger_direction_only: bool = False,
) -> ModelImportRelationRequest:
    return ModelImportRelationRequest(
        edges=edges,
        overlay=overlay,
        reference_time=REFERENCE_TIME,
        keep_stronger_direction_only=keep_stronger_direction_only,
    )
