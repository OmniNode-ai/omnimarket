# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic facts for the stability lane refresh tests."""

from __future__ import annotations

from omnimarket.nodes.node_stability_lane_refresh_effect.adapters import (
    InMemoryAppliedPinStore,
    InMemoryHealth,
    InMemoryPinSource,
    InMemoryTags,
    RecordingRunner,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.handlers.handler_stability_lane_refresh import (
    HandlerStabilityLaneRefresh,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.models import (
    ModelLaneHealthReading,
    ModelReleasePinSet,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.overlay import (
    ModelStabilityHostOverlay,
)

PIN = ModelReleasePinSet(
    omnibase_infra="v1.2.3", omnibase_core="v2.3.4", omnimarket="v3.4.5"
)
OTHER = ModelReleasePinSet(
    omnibase_infra="v1.2.4", omnibase_core="v2.3.4", omnimarket="v3.4.6"
)
PIN_TEXT = """\
schema_version: 1
lane: example-lane
host: example-box
date: 2026-01-02
bumped_by: example owner
pin:
  omnibase_infra: v1.2.3
  omnibase_core: v2.3.4
  omnibase_compat: v9.9.9
  omnimarket: v3.4.5
previous: null
"""
ALL_TAGS = {(repo, tag) for ps in (PIN, OTHER) for repo, tag in ps.as_pairs()}


def overlay(**changes: object) -> ModelStabilityHostOverlay:
    values: dict[str, object] = {
        "lane": "example-lane",
        "box_id": "example-box",
        "probe_host": "example.invalid",
        "health_port": 18085,
        "expected_environment": "example-env",
        "ports": {"EXAMPLE_PORT": 15436},
        "caps": {"EXAMPLE_CAP": "1024M"},
        "consumer_groups": {"EXAMPLE_GROUP": "example-box"},
        "workspace_config_root": "/example/config",
        "registry_root": "/example/home",
        "pin_file": "/example/config/pin.yaml",
        "refresh_script": "/example/home/refresh.sh",
        "state_dir": "/example/state",
        "deploy_reason": "OMN-0: example",
        "refresh_timeout_seconds": 60,
        "health_timeout_seconds": 5,
    }
    values.update(changes)
    return ModelStabilityHostOverlay.model_validate(values)


class Rig:
    """A handler over in-memory adapters, with the recording runner exposed."""

    def __init__(
        self,
        *,
        pin_text: str = PIN_TEXT,
        applied: ModelReleasePinSet | None = None,
        tags: set[tuple[str, str]] | None = None,
        health: ModelLaneHealthReading | None = None,
        runner: RecordingRunner | None = None,
    ) -> None:
        self.runner = runner or RecordingRunner()
        self.store = InMemoryAppliedPinStore(applied)
        self.handler = HandlerStabilityLaneRefresh(
            overlay=overlay(),
            pin_source=InMemoryPinSource(pin_text),
            applied=self.store,
            runner=self.runner,
            tags=InMemoryTags(ALL_TAGS if tags is None else tags),
            health=InMemoryHealth(health),
        )
