# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime wires the node on a lane that holds no Dharma secret (OMN-20090).

The runtime's auto-wiring drops every contract whose ``required: true`` secrets
its store does not hold (OMN-21423). The Dharma secrets are read only under the
``dharma_full`` setting, so a lane without them (``off`` or ``in_memory``) must
still join the node's consumer group; the handler refuses ``dharma_full``
without them as ``backend_misconfigured`` instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_infra.runtime.auto_wiring.discovery import (
    discover_contracts_from_paths,
    gate_manifest_on_required_secrets,
)

import omnimarket.nodes.node_trajectory_evaluation_effect as node_pkg

pytestmark = pytest.mark.unit

CONTRACT = Path(node_pkg.__file__).parent / "contract.yaml"


def test_node_survives_the_required_secret_gate_without_dharma_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_ACTIVE_RUNTIME_PACKAGES", raising=False)
    manifest = discover_contracts_from_paths([CONTRACT])
    assert [c.name for c in manifest.contracts] == ["node_trajectory_evaluation_effect"]
    assert manifest.contracts[0].required_secrets == ()

    gated = gate_manifest_on_required_secrets(manifest, frozenset())

    assert [c.name for c in gated.contracts] == ["node_trajectory_evaluation_effect"]
    assert not [s for s in gated.skips if s.reason == "required_secret_absent"]
