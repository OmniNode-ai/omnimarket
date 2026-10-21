# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21208: a harness-tier delegate-skill terminal gets its modelled saving row.

The harness-delegate skill publishes its own delegate-skill terminal. Its
``metrics`` carry the served tokens and a recorded ``cost_usd`` of 0.0 but no
``cost_savings_usd`` and no baseline, so ``from_terminal_event`` returned no row
at its first branch (``metrics.cost_savings_usd is None``). The dev lane read
2026-10-21 showed 6,150 harness runs over three days, every one with tokens and
none with a saving, while the Runs page showed each one's modelled saving. Both
savings totals therefore left them out.

The payload below has the harness-delegate skill's exact terminal key set, with
the token counts of recorded run 001c0787-9f23-4718-b8b2-a5dc8a4900fa and its
prompt and answer replaced by short text. The expected saving is priced here
from the manifest entry directly, not through the code under test.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillSavingsProjection,
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

TOPIC = "onex.evt.omnimarket.delegate-skill-completed.v1"
HOUSE_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
BASELINE = "claude-sonnet-5-5"
TOKENS_IN = 13419
TOKENS_OUT = 124


def _harness_terminal(**overrides: Any) -> dict[str, Any]:
    run_id = str(uuid4())
    payload: dict[str, Any] = {
        "status": "completed",
        "correlation_id": run_id,
        "task_type": "reasoning",
        "provider": "harness:claude-glm",
        "model_name": "glm-5.3-flash",
        "prompt_text": "Write four short sentences.",
        "prompt_chars": 27,
        "prompt_sha256": "0" * 64,
        "response": "Four short sentences.",
        "response_chars": 21,
        "response_sha256": "1" * 64,
        "quality_gate_passed": True,
        "quality_score": 1.0,
        "error_message": "",
        "metrics": {
            "input_tokens": TOKENS_IN,
            "output_tokens": TOKENS_OUT,
            "total_tokens": TOKENS_IN + TOKENS_OUT,
            "latency_ms": 6799,
            "cost_usd": 0.0,
        },
        "attempts": [
            {
                "tier": "harness",
                "backend_id": "harness:claude-glm:glm-5.3-flash",
                "model_id": "glm-5.3-flash",
                "quality_gate_passed": True,
                "acceptance_decision": "accept",
                "acceptance_reason": "quality_bar_met",
                "failure_class": None,
                "error_message": "",
            }
        ],
        "attempts_count": 1,
        "escalation_count": 0,
        "session_id": run_id,
        "delegated_to": "glm-5.3-flash",
        "caller_lane": "omn21208-test",
        "ticket_id": "OMN-21208",
        "tenant_id": HOUSE_TENANT,
        "emitted_at": "2026-10-21T04:56:53.587316+00:00",
    }
    payload.update(overrides)
    return payload


def _expected_saving(tokens_in: int, tokens_out: int, cost: Decimal) -> Decimal:
    entry = ModelPricingTable.from_yaml().get_entry(BASELINE)
    assert entry is not None, "the baseline must be priced in the manifest"
    counterfactual = (
        Decimal(str(entry.input_cost_per_1k)) * tokens_in
        + Decimal(str(entry.output_cost_per_1k)) * tokens_out
    ) / Decimal(1000)
    return counterfactual - cost


def _apply(payload: dict[str, Any]) -> AsyncMock:
    runner = SavingsProjectionRunner()
    upsert = AsyncMock()
    meta = MessageMeta(topic=TOPIC, partition=0, offset=1, fallback_id=str(uuid4()))
    with patch.object(runner, "_upsert_run_savings", upsert):
        ok = asyncio.run(
            runner._apply_event(TOPIC, payload, meta, write_tenant=HOUSE_TENANT)
        )
    assert ok is True
    return upsert


def _written(upsert: AsyncMock) -> ModelDelegateSkillSavingsProjection:
    call = upsert.await_args
    assert call is not None, "the runner wrote no savings row"
    row = call.args[0]
    assert isinstance(row, ModelDelegateSkillSavingsProjection)
    return row


def test_harness_terminal_reports_no_saving_so_the_reported_path_writes_nothing() -> (
    None
):
    """AC1: the cause. The terminal decodes, carries tokens, and reports no saving."""
    terminal = ModelDelegateSkillTerminalProjection.from_payload(_harness_terminal())
    assert terminal.metrics.input_tokens == TOKENS_IN
    assert terminal.metrics.cost_savings_usd is None
    assert terminal.baseline_state == "RESOLVED"
    assert (
        ModelDelegateSkillSavingsProjection.from_terminal_event(
            terminal, baseline_model=BASELINE
        )
        is None
    )


def test_harness_terminal_writes_its_modelled_saving_row() -> None:
    """AC2: one row, priced from the served tokens at the baseline's list price."""
    payload = _harness_terminal()
    upsert = _apply(payload)
    assert upsert.await_count == 1
    row = _written(upsert)
    expected = _expected_saving(TOKENS_IN, TOKENS_OUT, Decimal("0"))
    assert expected > 0
    assert row.savings_usd == expected
    assert row.local_cost_usd == Decimal("0")
    assert row.cloud_cost_usd == expected
    assert row.model_cloud_baseline == BASELINE
    assert row.pricing_manifest_version > 0
    assert row.model_local == "glm-5.3-flash"
    assert str(row.session_id) == payload["correlation_id"]
    assert (row.prompt_tokens, row.completion_tokens) == (TOKENS_IN, TOKENS_OUT)
    assert row.tenant_id == HOUSE_TENANT
    assert row.savings_method == "measured"


def test_recorded_cost_is_subtracted() -> None:
    payload = _harness_terminal()
    payload["metrics"] = {**payload["metrics"], "cost_usd": 0.01}
    row = _written(_apply(payload))
    assert row.savings_usd == _expected_saving(TOKENS_IN, TOKENS_OUT, Decimal("0.01"))
    assert row.local_cost_usd == Decimal("0.01")


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        (
            "no served tokens",
            {
                "metrics": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "latency_ms": 1,
                    "cost_usd": 0.0,
                }
            },
        ),
        ("gate failed", {"quality_gate_passed": False, "quality_score": 0.0}),
        (
            "not completed",
            {"status": "failed", "error_message": "provider_call_failed"},
        ),
        ("baseline unresolved", {"baseline_state": "BASELINE_UNRESOLVED"}),
    ],
)
def test_no_row_without_a_modelled_saving(
    label: str, overrides: dict[str, Any]
) -> None:
    assert _apply(_harness_terminal(**overrides)).await_count == 0, label


def test_a_reported_saving_still_wins() -> None:
    """A terminal that states its own saving keeps it; the modelled path is a fallback."""
    payload = _harness_terminal()
    payload["metrics"] = {**payload["metrics"], "cost_savings_usd": 0.5}
    row = _written(_apply(payload))
    assert row.savings_usd == Decimal("0.5")
