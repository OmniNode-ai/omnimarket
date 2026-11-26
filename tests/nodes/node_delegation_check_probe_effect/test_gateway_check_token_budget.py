# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A gateway check's healthy submission carries an output budget a real model can finish under (OMN-26043).

Run c29-20261126T044722Z-f2c5a20f answered under the tenant's own OpenRouter key and still read
FAIL on completed_under_tenant_key: the workflow failed with "API response truncated:
finish_reason=length" because the probe asked for 32 output tokens. The gateway refusing a
truncated answer is the product working (OMN-18278); the probe's budget was the defect. The
control run 275d746c (max_tokens 128) completed; 8 and fewer failed the same way.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_golden_answers_gateway_graders import (
    GATEWAY_CHECKS,
    _real,
    _run,
)

MEASURED_COMPLETING_BUDGET = 128


@pytest.mark.parametrize("check", GATEWAY_CHECKS)
def test_every_submission_carries_a_budget_a_real_model_completes_under(
    check: str,
) -> None:
    gateway = _real("api_key")
    _run(check, gateway)
    sent = [s["body"] for s in gateway.sent if s["path"] == "/v1/workflows"]
    for body in sent:
        assert body["payload"]["max_tokens"] >= MEASURED_COMPLETING_BUDGET


def test_the_tenant_key_check_submits_so_the_budget_assertion_reads_something() -> None:
    gateway = _real("api_key")
    _run("C29", gateway)
    assert [s for s in gateway.sent if s["path"] == "/v1/workflows"]
