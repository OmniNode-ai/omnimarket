# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployed-lane routing consumer reads the DoD pass rate (OMN-19528, AC3).

``HandlerRoutingIntent`` is the routing consumer the deployed runtime runs for
every delegation (``onex.cmd.omnibase-infra.delegation-routing-request.v1``).
Before OMN-19528 it passed no outcome overlay to the reducer at all, so no
stored outcome could move a deployed-lane decision. These tests pin that it now
threads the DoD overlay in, cites the rate in one log line, moves off a
suppressed tier, and decides exactly as before when no reader is wired.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    ModelDelegationRequest,
    ModelRoutingIntent,
)

from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from tests.unit.delegation.test_roi_overlay_routing_omn14001 import (
    _BIFROST_THREE_TIER,
)

TENANT_ID = "820272f9-4aaf-5add-a2df-0af942852ab2"
TASK_TYPE = "code_generation"


@pytest.fixture(autouse=True)
def _three_tier_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_THREE_TIER)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
    for name in (
        "DELEGATION_ROI_MIN_SAMPLES",
        "DELEGATION_ROI_SUCCESS_FLOOR",
        "DELEGATION_ROI_LOOKBACK_ROWS",
        "DELEGATION_ROI_WINDOW_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()


def _joined(
    *, outcome: str, tier: str = "local", model: str = "Qwen3.8-27B"
) -> dict[str, object]:
    return {
        "task_type": TASK_TYPE,
        "correlation_id": str(uuid4()),
        "tier_name": tier,
        "model_name": model,
        "created_at": datetime(2026, 9, 25, 3, 0, tzinfo=UTC),
        "verdict_correlation_id": str(uuid4()),
        "verdict_status": "verified" if outcome == "done" else "failed",
        "verdict_outcome": outcome,
        "verdict_completed_at": datetime(2026, 9, 25, 3, 5, tzinfo=UTC),
    }


class _Reader:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, str]] = []

    def read_dod_outcomes(
        self, *, task_type: str, tenant_id: str
    ) -> list[dict[str, object]]:
        self.calls.append((task_type, tenant_id))
        return [r for r in self.rows if r["task_type"] == task_type]


class _NoTenantOverlay:
    def query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        return []


def _intent(tenant_id: str | None = TENANT_ID) -> ModelRoutingIntent:
    request = ModelDelegationRequest(
        prompt="x" * 100,
        task_type=TASK_TYPE,
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
        tenant_id=tenant_id,
    )
    return ModelRoutingIntent(payload=request)


@pytest.mark.unit
def test_no_reader_wired_decides_exactly_as_before() -> None:
    handler = HandlerRoutingIntent(tenant_overlay_db=_NoTenantOverlay())
    intent = _intent()

    decision = handler.handle(intent)
    static = routing.delta(intent.payload, surface=routing.EnumDelegationSurface.CLOUD)

    assert decision.tier_name == static.tier_name == "local"
    assert decision.rationale == static.rationale


@pytest.mark.unit
def test_dod_suppressed_tier_moves_the_deployed_decision() -> None:
    reader = _Reader([_joined(outcome="refused") for _ in range(5)])
    handler = HandlerRoutingIntent(
        tenant_overlay_db=_NoTenantOverlay(), dod_outcome_reader=reader
    )

    decision = handler.handle(_intent())

    assert reader.calls == [(TASK_TYPE, TENANT_ID)]
    assert decision.tier_name != "local"
    assert "ROI-demoted past ['local']" in decision.rationale


@pytest.mark.unit
def test_passing_tier_keeps_the_static_decision() -> None:
    reader = _Reader([_joined(outcome="done") for _ in range(5)])
    handler = HandlerRoutingIntent(
        tenant_overlay_db=_NoTenantOverlay(), dod_outcome_reader=reader
    )

    decision = handler.handle(_intent())

    assert decision.tier_name == "local"


@pytest.mark.unit
def test_one_log_line_cites_each_tier_model_rate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows = [_joined(outcome="done"), _joined(outcome="refused")]
    rows += [_joined(outcome="refused", tier="cheap_cloud", model="glm-5.3-flash")]
    handler = HandlerRoutingIntent(
        tenant_overlay_db=_NoTenantOverlay(), dod_outcome_reader=_Reader(rows)
    )
    intent = _intent()

    with caplog.at_level(logging.INFO):
        handler.handle(intent)

    lines = [
        r.getMessage() for r in caplog.records if "DoD routing read" in r.getMessage()
    ]
    assert len(lines) == 1
    line = lines[0]
    assert str(intent.payload.correlation_id) in line
    assert f"task_type={TASK_TYPE}" in line
    assert "local/Qwen3.8-27B n=2 pass=1 rate=0.500" in line
    assert "cheap_cloud/glm-5.3-flash n=1 pass=0 rate=0.000" in line
    assert "joined_verdicts=3 undecided=0" in line


@pytest.mark.unit
def test_log_line_counts_joined_verdicts_that_decided_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    skipped = _joined(outcome="refused")
    skipped["verdict_status"] = "skipped"
    handler = HandlerRoutingIntent(
        tenant_overlay_db=_NoTenantOverlay(), dod_outcome_reader=_Reader([skipped])
    )

    with caplog.at_level(logging.INFO):
        decision = handler.handle(_intent())

    (line,) = [
        r.getMessage() for r in caplog.records if "DoD routing read" in r.getMessage()
    ]
    assert "no decided DoD verdicts" in line
    assert "joined_verdicts=1 undecided=1" in line
    assert decision.tier_name == "local"


@pytest.mark.unit
def test_read_failure_falls_back_to_the_static_decision() -> None:
    class _Broken:
        def read_dod_outcomes(
            self, *, task_type: str, tenant_id: str
        ) -> list[dict[str, object]]:
            raise ConnectionError("projection DB unreachable")

    handler = HandlerRoutingIntent(
        tenant_overlay_db=_NoTenantOverlay(), dod_outcome_reader=_Broken()
    )

    assert handler.handle(_intent()).tier_name == "local"


@pytest.mark.unit
def test_request_without_tenant_reads_under_the_lane_tenant() -> None:
    reader = _Reader([])
    handler = HandlerRoutingIntent(
        tenant_overlay_db=_NoTenantOverlay(), dod_outcome_reader=reader
    )

    handler.handle(_intent(tenant_id=None))

    assert reader.calls == [(TASK_TYPE, TENANT_ID)]
