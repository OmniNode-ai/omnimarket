# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed delegation terminal in the ``onex delegate`` output surfaces as a lane failure (OMN-23822).

A delegate output whose status is the contract's failed status used to be read as if only the correlation id
mattered, so a delegation that ended failed was reported as if it had been sent. The lane now raises a
``LaneReadError`` naming the failure reason, through the real ``BusLane`` built by the production wiring
``handler_scenario_r8._build_bus_lane``; its delegate command is a fake that prints the output under test.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from uuid import UUID

import pytest

from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioTenantReadOverlay,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers import (
    handler_scenario_r8,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    LaneReadError,
    ModelSendRequest,
    ProtocolR8Lane,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    load_lane_settings,
)

pytestmark = pytest.mark.unit

TENANT = UUID("11111111-1111-1111-1111-111111111111")
SETTINGS = load_lane_settings()
KAFKA_OVERLAY = ModelScenarioTenantReadOverlay(
    bus="kafka", bus_lane="dev", probe_tenant_id=TENANT, timeout_seconds=5.0
)
REASON = (
    "[ONEX_MARKET_CUSTOMER_PROVIDER_KEY_ABSENT] "
    "no provider key is registered for this tenant"
)


def _request() -> ModelSendRequest:
    return ModelSendRequest(
        task_type="summarization",
        prompt="p",
        traffic_class="synthetic",
        source_surface="readiness_probe",
    )


def _lane_with_delegate_output(
    monkeypatch: pytest.MonkeyPatch, output: str
) -> ProtocolR8Lane:
    """The real bus lane whose delegate command prints ``output``."""
    assert SETTINGS.bus is not None

    def fake_run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(list(argv), 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return handler_scenario_r8._build_bus_lane(
        SETTINGS, {SETTINGS.bus.workspace_env: "/workspace"}, KAFKA_OVERLAY
    )


def _nested(path: str, value: str) -> dict[str, object]:
    """``value`` at the dotted ``path`` of nested mappings, the shape the deployed lane's delegate answer carries."""
    *parents, leaf = path.split(".")
    inner: dict[str, object] = {leaf: value}
    for parent in reversed(parents):
        inner = {parent: inner}
    return inner


def _delegate_output(reason_path: str | None = None) -> str:
    """A failed delegate output carrying ``REASON`` at ``reason_path``, or no reason at all."""
    body: dict[str, object] = {
        SETTINGS.delegate_id_key: "run-1",
        SETTINGS.delegate_status_key: SETTINGS.delegate_failed_status,
    }
    if reason_path is not None:
        body.update(_nested(reason_path, REASON))
    return json.dumps(body)


def test_golden_a_delegate_output_that_did_not_end_failed_returns_its_correlation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delegate output whose status is not the failed one still returns the correlation id."""
    output = json.dumps(
        {
            SETTINGS.delegate_id_key: "run-1",
            SETTINGS.delegate_status_key: "success",
        }
    )
    lane = _lane_with_delegate_output(monkeypatch, output)
    assert lane.send(_request()) == "run-1"


def test_error_chain_a_failed_delegate_output_names_its_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delegate output whose status is the failed one raises a LaneReadError naming the reason."""
    lane = _lane_with_delegate_output(
        monkeypatch,
        _delegate_output(SETTINGS.delegate_reason_keys[0]),
    )
    with pytest.raises(LaneReadError) as raised:
        lane.send(_request())
    assert "ended failed" in str(raised.value)
    assert REASON in str(raised.value)


def test_error_chain_without_the_first_reason_key_the_fallback_key_carries_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the first declared reason key, the next declared key carries the reason."""
    lane = _lane_with_delegate_output(
        monkeypatch,
        _delegate_output(SETTINGS.delegate_reason_keys[1]),
    )
    with pytest.raises(LaneReadError) as raised:
        lane.send(_request())
    assert "ended failed" in str(raised.value)
    assert REASON in str(raised.value)


def test_error_chain_a_failed_delegate_output_without_a_reason_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed delegate output that names no declared reason key says no reason was given."""
    lane = _lane_with_delegate_output(monkeypatch, _delegate_output())
    with pytest.raises(LaneReadError) as raised:
        lane.send(_request())
    assert "ended failed" in str(raised.value)
    assert "no reason given" in str(raised.value)
    assert REASON not in str(raised.value)
