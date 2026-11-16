# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The remote accounts read resolves its registry root from the declared lab host table, never from the local environment (OMN-23664).

Golden: the shipped redpanda profile, wired with a declared registry root, hands the far host an ssh argv that walks that root.
Error: a profile that names {registry_roots} with no root declared is refused at wiring, so the unit ends in the typed failed event
instead of reading every host as zero slots.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_ports import (
    InMemoryPoolSnapshotPublisher,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_wiring import (
    build_pool_snapshot_ports,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot_overlay import (
    ModelPoolSnapshotOverlay,
)
from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import host_row

pytestmark = pytest.mark.unit

PROFILE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lane_host_pool_snapshot_effect/overlay.redpanda.example.yaml"
)


def _profile() -> ModelPoolSnapshotOverlay:
    return ModelPoolSnapshotOverlay.model_validate(
        yaml.safe_load(PROFILE.read_text(encoding="utf-8"))
    )


def test_wiring_hands_a_far_host_the_declared_registry_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path))
    calls: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], timeout: int) -> tuple[int, str]:
        calls.append(tuple(argv))
        return 1, "refused"

    ports = build_pool_snapshot_ports(
        _profile(),
        environ={
            "EXAMPLE_LEDGER_READ_COMMAND": "true",
            "ONEX_REGISTRY_ROOT": "/declared/registry",
        },
        runner=runner,
        publisher=InMemoryPoolSnapshotPublisher(),
    )
    ports.accounts.read_accounts((host_row("h2"),))
    assert len(calls) == 1
    assert calls[0][:2] == ("ssh", "h2")
    assert 'for r in "/declared/registry"; do' in calls[0][2]
    # the local environment's path is never written into the far command
    assert str(tmp_path) not in calls[0][2]
    assert "{registry_roots}" not in calls[0][2]


def test_error_wiring_refuses_a_profile_with_no_declared_registry_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIBASE_PATH", str(tmp_path))
    with pytest.raises(ValueError, match="registry roots unreadable"):
        build_pool_snapshot_ports(
            _profile(),
            environ={"EXAMPLE_LEDGER_READ_COMMAND": "true"},
            publisher=InMemoryPoolSnapshotPublisher(),
        )
