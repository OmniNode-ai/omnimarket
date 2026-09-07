# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The real ADR manifests load under the shared entry model (OMN-14103, Gap 2).

``discovery_manifest.yaml`` entries omit ``ground_truth_adr`` and carry
discovery-only fields; ground-truth entries are strict benchmark entries. Both
must validate against ``ModelAdrManifestEntry`` / ``ModelGroundTruthManifest`` —
before this fix the discovery manifest raised on every entry, so the discovery
corpus could never be run.

The strict half now loads a synthetic fixture rather than the real corpus,
which OMN-18026 moved out of this public repository. The property under test is
unchanged: a benchmark entry must carry ground-truth text and must not be in
discovery mode.
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
# The real ground-truth corpus left this public repository under OMN-18026 (it
# declared itself restricted and inlined 60 authoritative ADRs). The strict
# benchmark shape is still worth pinning, so it is pinned against a synthetic
# manifest of invented ADRs instead of the private corpus.
_SYNTHETIC_GROUND_TRUTH = (
    _REPO_ROOT
    / "tests"
    / "fixtures"
    / "adr_canary"
    / "ground_truth_manifest_synthetic.v1.yaml"
)


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
    raw = yaml.safe_load(_SYNTHETIC_GROUND_TRUTH.read_text(encoding="utf-8"))
    manifest = ModelGroundTruthManifest.model_validate(raw)

    assert len(manifest.entries) >= 1
    for entry in manifest.entries:
        # Benchmark entries stay strict: ground truth present, discovery off.
        assert entry.discovery_mode is False, entry.id
        assert entry.ground_truth_adr is not None, entry.id
        assert entry.ground_truth_adr.strip(), entry.id
