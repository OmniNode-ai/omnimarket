# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged example overlay has the shape the node's model loads (OMN-23612); the deployment's document is the overlays
repository's, which loads its own through the same model at a pinned revision."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_pr_handoff_lab_exempt_compute.models.model_install_declaration import (
    ModelPrHandoffReadsOverlay,
)

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_pr_handoff_reads_effect/overlay.example.yaml"
)


def test_the_example_overlay_loads_through_the_model() -> None:
    document = ModelPrHandoffReadsOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )
    assert "scripts/ci/*" in document.host_install.excluded_paths


def test_a_key_the_model_does_not_know_is_refused() -> None:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["host_install"]["surprise"] = True
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelPrHandoffReadsOverlay.model_validate(raw)
