# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The canonical result must cross the served-text boundary without exponents.

The page deliberately accepts plain decimal text only. Decimal.__str__ emits
exponents for small measured figures and scaled zero, making those real
measurements look unmeasured. The fixture pins pricing only; the real adapter
and metering compute produce each result, including twenty significant digits.
"""

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_run_savings import (
    modelled_run_savings_columns,
)
from tests.projection.test_omn21015_run_savings_served import priced as priced


@pytest.mark.usefixtures("priced")
@pytest.mark.parametrize(
    ("spend", "expected"),
    [
        pytest.param("0.01399990", "0.00000010", id="small-measured-figure"),
        pytest.param("0.01400000", "0.00000000", id="measured-scaled-zero"),
        pytest.param(
            "0.01399987654321098765432110",
            "0.00000012345678901234567890",
            id="twenty-significant-digits",
        ),
    ],
)
def test_run_saving_is_served_as_exact_fixed_point_text(
    spend: str, expected: str
) -> None:
    result = modelled_run_savings_columns(
        {
            "correlation_id": "decimal-boundary-control",
            "created_at": datetime(2026, 10, 13, tzinfo=UTC),
            "tokens_input": 1000,
            "tokens_output": 200,
            "cost_usd": spend,
            "delegated_to": "fixture-serving-model",
            "task_type": "document",
        }
    )
    assert result["savings_measurement"] == "measured"
    assert result["savings_baseline_model"] == "fixture-baseline"
    assert result["savings_pricing_manifest_version"] == "fixture-v1"
    assert result["modelled_savings_usd"] == expected
