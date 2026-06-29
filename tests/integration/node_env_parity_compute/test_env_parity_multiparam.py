# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_env_parity_compute (OMN-13676).

COMPUTE node, Variant A: the handler is pure — it takes a typed synthetic
``env_by_lane`` snapshot and computes parity against the contract's declared
variables. No process environment is read, so every param set is fully
deterministic in CI. Each case asserts typed result fields (status, parity_ok,
gap reasons); the negative-control cases (b/c/d) must produce a concrete gap.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_env_parity_compute.handlers.handler_env_parity_compute import (
    HandlerEnvParityCompute,
)
from omnimarket.nodes.node_env_parity_compute.models.model_env_parity_compute_request import (
    ModelEnvParityComputeRequest,
)

_KAFKA_VARS = ["ENABLE_KAFKA", "KAFKA_BOOTSTRAP_SERVERS", "KAFKA_CONSUMER_GROUP"]


# (id, request_kwargs, expected_status, expected_parity_ok, required_gap_reason)
CASES = [
    pytest.param(
        {
            "variable_names": _KAFKA_VARS,
            "env_by_lane": {
                "dev": {"ENABLE_KAFKA": "false"},
                "staging": {"ENABLE_KAFKA": "false"},
                "prod": {"ENABLE_KAFKA": "false"},
            },
        },
        "passed",
        True,
        None,
        id="parity-clean-all-lanes-disabled",
    ),
    pytest.param(
        {
            "lanes": ["dev"],
            "variable_names": ["ENABLE_KAFKA"],
            "env_by_lane": {"dev": {"ENABLE_KAFKA": "false"}},
        },
        "passed",
        True,
        None,
        id="parity-clean-lane-subset",
    ),
    pytest.param(
        {
            "variable_names": _KAFKA_VARS,
            "env_by_lane": {
                "dev": {"ENABLE_KAFKA": "true"},
                "staging": {"ENABLE_KAFKA": "true"},
                "prod": {"ENABLE_KAFKA": "true"},
            },
        },
        "gaps_detected",
        False,
        "missing_required_env",
        id="missing-required-kafka-bootstrap",
    ),
    pytest.param(
        {
            "variable_names": _KAFKA_VARS,
            "env_by_lane": {
                "dev": {
                    "ENABLE_KAFKA": "true",
                    "KAFKA_BOOTSTRAP_SERVERS": "dev:9092",
                    "KAFKA_CONSUMER_GROUP": "g1",
                },
                "staging": {"ENABLE_KAFKA": "false"},
                "prod": {"ENABLE_KAFKA": "false"},
            },
        },
        "gaps_detected",
        False,
        "value_mismatch",
        id="fingerprint-divergence-across-lanes",
    ),
    pytest.param(
        {
            "variable_names": ["ENABLE_KAFKA"],
            "env_by_lane": {
                "dev": {"ENABLE_KAFKA": "false"},
                "staging": {"ENABLE_KAFKA": "false"},
            },
        },
        "gaps_detected",
        False,
        "lane_missing",
        id="lane-snapshot-missing-for-prod",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("request_kwargs", "expected_status", "expected_parity_ok", "required_gap_reason"),
    [(c.values[0], c.values[1], c.values[2], c.values[3]) for c in CASES],
    ids=[c.id for c in CASES],
)
def test_env_parity_multiparam(
    request_kwargs: dict[str, object],
    expected_status: str,
    expected_parity_ok: bool,
    required_gap_reason: str | None,
) -> None:
    correlation_id = uuid4()
    request = ModelEnvParityComputeRequest(
        correlation_id=correlation_id, **request_kwargs
    )
    result = HandlerEnvParityCompute().handle(request)

    # Typed result fields, not "no exception".
    assert result.status == expected_status
    assert result.parity_ok is expected_parity_ok
    assert result.correlation_id == correlation_id
    assert result.error is None
    # The handler always echoes the lanes it actually checked.
    assert result.lanes_checked, "expected at least one lane checked"

    if required_gap_reason is None:
        assert result.gaps == [], f"expected no gaps, got {result.gaps}"
    else:
        # Negative control: a concrete gap of the expected kind must be produced.
        reasons = {gap.reason for gap in result.gaps}
        assert required_gap_reason in reasons, (
            f"expected gap reason {required_gap_reason!r}, got {sorted(reasons)}"
        )
        # Every gap must carry structured fields (real finding, not a placeholder).
        for gap in result.gaps:
            assert gap.lane
            assert gap.variable_name
            assert gap.reason
