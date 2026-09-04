# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The real ADR manifests load under the shared entry model (OMN-14103, Gap 2).

``discovery_manifest.yaml`` entries omit ``ground_truth_adr`` and carry
discovery-only fields; ``ground_truth_manifest.yaml`` entries are strict
benchmark entries. Both must validate against ``ModelAdrManifestEntry`` /
``ModelGroundTruthManifest`` — before this fix the discovery manifest raised on
every entry, so the discovery corpus could never be run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_adr_canary_orchestrator.handlers.handler_canary_orchestrator import (
    ModelGroundTruthManifest,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CANARY_CONFIGS = _REPO_ROOT / "src" / "omnimarket" / "configs"


@pytest.mark.unit
def test_discovery_manifest_loads_all_entries() -> None:
    raw = yaml.safe_load(
        (_CANARY_CONFIGS / "adr_canary_discovery_manifest.v1.yaml").read_text(
            encoding="utf-8"
        )
    )
    manifest = ModelGroundTruthManifest.model_validate(raw)

    assert len(manifest.entries) >= 37
    for entry in manifest.entries:
        assert entry.discovery_mode is True, entry.id
        assert entry.ground_truth_adr is None, entry.id
        assert entry.root_paths, entry.id
        assert entry.models, entry.id


@pytest.mark.unit
def test_ground_truth_manifest_still_loads_strict() -> None:
    raw = yaml.safe_load(
        (_CANARY_CONFIGS / "adr_canary_ground_truth_manifest.v1.yaml").read_text(
            encoding="utf-8"
        )
    )
    manifest = ModelGroundTruthManifest.model_validate(raw)

    assert len(manifest.entries) >= 1
    for entry in manifest.entries:
        # Benchmark entries stay strict: ground truth present, discovery off.
        assert entry.discovery_mode is False, entry.id
        assert entry.ground_truth_adr is not None, entry.id
        assert entry.ground_truth_adr.strip(), entry.id
