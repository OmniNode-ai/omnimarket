# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Small, fixed sampling populations pin selection and shortfall boundaries."""

import pytest

from omnimarket.nodes.node_delegation_eval_sample_compute.handlers.handler_delegation_eval_sample import (
    HandlerDelegationEvalSample,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_sample_request import (
    ModelDelegationEvalSampleRequest,
)

pytestmark = pytest.mark.unit


def _request(count: int) -> ModelDelegationEvalSampleRequest:
    return ModelDelegationEvalSampleRequest.model_validate(
        {
            "seed": "edge-seed",
            "window_start": "2026-09-01T00:00:00Z",
            "window_end": "2026-09-02T00:00:00Z",
            "query_text": "SELECT committed identifiers",
            "house_tenant_id": "house",
            "sampling": {
                "quotas": [
                    {"name": "accepted", "count": 3},
                    {"name": "refused", "count": 2},
                    {"name": "undetermined", "count": 1},
                ],
                "holdout_buckets": 10,
                "reserved_bucket": 0,
                "order_key": "sha256(seed + correlation_id + attempt_index)",
            },
            # These identities all hash outside the reserved holdout bucket.
            "candidates": [
                {
                    "correlation_id": f"edge-{index}",
                    "attempt_index": 0,
                    "tenant_id": "house",
                    "task_class": "summarization",
                    "gate_outcome": "accepted",
                }
                for index in range(count)
            ],
        }
    )


def test_empty_population_has_no_items_or_invented_shortfalls() -> None:
    request = _request(0)
    result = HandlerDelegationEvalSample().handle(request)

    assert result.items == ()
    assert result.shortfalls == ()
    assert result.rejected_imports == ()
    assert result.excluded_holdout_bucket == result.excluded_customer_tenant == 0
    assert result.quotas == request.sampling.quotas
    assert result == HandlerDelegationEvalSample().handle(request)


def test_population_below_quota_takes_every_item_once() -> None:
    result = HandlerDelegationEvalSample().handle(_request(2))

    assert [item.key.correlation_id for item in result.items] == ["edge-1", "edge-0"]
    assert all(item.source == "drawn" for item in result.items)
    assert [row.model_dump() for row in result.shortfalls] == [
        {"stratum": "summarization/accepted", "quota": 3, "available": 2, "taken": 2}
    ]


def test_fixed_seed_pins_pick_across_redelivery_and_input_permutations() -> None:
    request = _request(8)
    handler = HandlerDelegationEvalSample()
    result = handler.handle(request)

    # A literal golden pick catches changes to seed serialization or sort order.
    assert [item.key.correlation_id for item in result.items] == [
        "edge-2",
        "edge-3",
        "edge-6",
    ]
    assert result.shortfalls == ()
    assert result == handler.handle(request)
    assert result == handler.handle(
        request.model_copy(update={"candidates": tuple(reversed(request.candidates))})
    )
