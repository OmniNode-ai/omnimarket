# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the host-drift operations through their packaged contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes import node_launchd_drift_effect
from omnimarket.nodes.node_launchd_drift_effect.models.enum_host_drift_verdict import (
    EnumHostDriftVerdict,
)
from omnimarket.nodes.node_launchd_drift_effect.models.model_host_drift_escalate_item import (
    ModelHostDriftEscalateItem,
)
from omnimarket.nodes.node_launchd_drift_effect.models.model_host_drift_item import (
    ModelHostDriftItem,
)
from omnimarket.nodes.node_launchd_drift_effect.models.model_host_drift_observation import (
    ModelHostDriftObservation,
)
from omnimarket.nodes.node_launchd_drift_effect.models.model_host_drift_state import (
    ModelHostDriftState,
)

pytestmark = pytest.mark.unit


def test_contract_routes_fold_and_escalation() -> None:
    assert node_launchd_drift_effect.__file__ is not None
    contract = yaml.safe_load(
        Path(node_launchd_drift_effect.__file__).with_name("contract.yaml").read_text()
    )
    routes = {
        route["operation"]: route for route in contract["handler_routing"]["handlers"]
    }
    now = datetime(2026, 10, 7, tzinfo=UTC)
    item = ModelHostDriftItem(
        "clone:x", "clone", EnumHostDriftVerdict.DRIFT, "dev", "behind", 60
    )
    fold_route = routes["fold"]
    fold_handler = getattr(
        import_module(fold_route["handler"]["module"]), fold_route["handler"]["name"]
    )()
    assert isinstance(fold_handler, node_launchd_drift_effect.HandlerHostDriftFold)
    fold_request = getattr(
        import_module(fold_route["event_model"]["module"]),
        fold_route["event_model"]["name"],
    )
    folded = fold_handler.handle(
        fold_request(
            ModelHostDriftState("test-host", None, ()),
            ModelHostDriftObservation("test-host", "lab", now, (item,)),
            "contract-readback",
            900,
        )
    )
    assert folded.detected[0].key == "clone:x"
    assert folded.heartbeat.items_drift == 1

    escalate_route = routes["escalate"]
    escalate_handler = getattr(
        import_module(escalate_route["handler"]["module"]),
        escalate_route["handler"]["name"],
    )()
    assert isinstance(
        escalate_handler, node_launchd_drift_effect.HandlerHostDriftEscalate
    )
    escalate_request = getattr(
        import_module(escalate_route["event_model"]["module"]),
        escalate_route["event_model"]["name"],
    )
    open_item = folded.state.open[0]
    escalation_item = ModelHostDriftEscalateItem(
        host=folded.state.host,
        key=open_item.key,
        kind=open_item.kind,
        verdict=open_item.verdict,
        observed=open_item.observed,
        first_seen_at=open_item.first_seen_at,
        bound_seconds=open_item.bound_seconds,
        apply_class="manual",
    )
    before = escalate_handler.handle(
        escalate_request((escalation_item,), now + timedelta(seconds=239))
    )
    assert not before.escalations
    at_bound = escalate_handler.handle(
        escalate_request((escalation_item,), now + timedelta(seconds=240))
    )
    assert at_bound.escalations[0].key == "clone:x"
    assert at_bound.escalations[0].escalate_after_seconds == 240
