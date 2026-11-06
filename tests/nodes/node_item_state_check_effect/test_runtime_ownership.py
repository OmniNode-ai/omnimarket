# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The item dispatch consumer belongs to the effects runtime at discovery."""

from pathlib import Path

from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.auto_wiring.profile_ownership import (
    filter_manifest_for_runtime_profile,
)


def test_item_state_effect_is_owned_only_by_effects_runtime() -> None:
    contract = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_item_state_check_effect/contract.yaml"
    )
    assert contract.is_file()
    discovered = discover_contracts_from_paths([contract])
    assert not discovered.errors
    assert len(discovered.contracts) == 1
    manifest = ModelAutoWiringManifest(contracts=tuple(discovered.contracts))
    effects = filter_manifest_for_runtime_profile(manifest, "effects", environ={})
    main = filter_manifest_for_runtime_profile(manifest, "main", environ={})
    assert not effects.manifest.errors
    assert len(effects.manifest.contracts) == 1
    assert not main.manifest.contracts
