# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: a local run states ONE saving, on the receipt and on the row.

The 2026-10-03 user-zero walk found a local ``delegation_events`` row storing
``cost_savings_usd`` 0.003645 while the same run's receipt said 0.000486. They
were two different quantities under one name: the receipt priced the run against
its resolved baseline model (``claude-sonnet-5-5``), the row carried the
effect handler's saving against a hardcoded Opus rate that no receipt names, with
no pinned counterfactual and a pricing manifest version of 0.

The transport is patched at the boundary (no network), as in
``test_local_dispatch_evidence``; the handler and the local port are real.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call,
    transport,
)
from omnimarket.pricing import estimate_baseline_cost_usd
from omnimarket.routing import delegation_backend_resolution

_TOKENS_IN = 213
_TOKENS_OUT = 6
#: The effect handler's hardcoded Opus rate: (213 * 15 + 6 * 75) / 1e6.
_OPUS_HARDCODED_SAVINGS = 0.003645


@pytest.fixture(autouse=True)
def _clear_health_cache() -> None:
    handler_llm_delegation_call._health_cache.clear()


def _patch_backend_and_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    backends: list[dict[str, object]] = [
        {
            "backend_id": "local-coder",
            "endpoint_url": "http://inference.example:8000/v1/chat/completions",
            "model_name": "Qwen3.6-35B-A3B",
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": 300000,
            "capabilities": ["code_generation"],
        }
    ]
    monkeypatch.setattr(
        delegation_backend_resolution, "load_bifrost_backends", lambda **_: backends
    )

    def fake_probe_health(endpoint_url: str, **_: Any) -> bool:
        return True

    def fake_post(
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
        runtime_profile: str | None = None,
    ) -> transport.ModelTransportResponse:
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [
                    {
                        "message": {
                            "content": "### ANSWER\ndef reverse(s): return s[::-1]"
                        }
                    }
                ],
                "model": "Qwen3.6-35B-A3B",
                "usage": {
                    "prompt_tokens": _TOKENS_IN,
                    "completion_tokens": _TOKENS_OUT,
                    "total_tokens": _TOKENS_IN + _TOKENS_OUT,
                },
            },
            latency_ms=42,
        )

    monkeypatch.setattr(transport, "probe_health", fake_probe_health)
    monkeypatch.setattr(transport, "post_chat_completion", fake_post)


async def _run_local_delegation(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, sqlite3.Row]:
    _patch_backend_and_transport(monkeypatch)
    port = LocalDelegationDispatchPort(
        evidence_db_path=db_path, effect_process_boundary=False
    )
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    response = await handler.handle(
        ModelDelegateSkillRequest(
            prompt="reverse a string",
            task_type="code_generation",
            source="claude-code",
        )
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (str(response.correlation_id),),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    return response, rows[0]


@pytest.mark.unit
async def test_row_and_receipt_state_the_same_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response, row = await _run_local_delegation(
        tmp_path / "delegation.sqlite", monkeypatch
    )

    assert response.status == "completed"
    receipt_savings = response.metrics.cost_savings_usd
    assert receipt_savings is not None
    # The figure is the baseline's price over the served tokens, not Opus's.
    counterfactual = estimate_baseline_cost_usd(
        prompt_tokens=_TOKENS_IN,
        completion_tokens=_TOKENS_OUT,
        baseline_model=response.model_cloud_baseline,
    )
    assert counterfactual is not None
    assert receipt_savings == pytest.approx(round(counterfactual, 6))
    assert receipt_savings != pytest.approx(_OPUS_HARDCODED_SAVINGS)

    assert row["cost_savings_usd"] == pytest.approx(receipt_savings)


@pytest.mark.unit
async def test_row_pins_the_counterfactual_its_saving_was_stated_against(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response, row = await _run_local_delegation(
        tmp_path / "delegation.sqlite", monkeypatch
    )

    pinned = json.loads(row["premium_counterfactual"])
    assert pinned["model"] == response.model_cloud_baseline
    assert pinned["tokens_in"] == _TOKENS_IN
    assert pinned["tokens_out"] == _TOKENS_OUT
    # The audit invariant: the stored saving reconciles with what the row pins.
    assert float(pinned["counterfactual_cost_usd"]) - row["cost_usd"] == pytest.approx(
        row["cost_savings_usd"]
    )
    assert row["pricing_manifest_version"] == response.pricing_manifest_version
    assert row["pricing_manifest_version"] != 0
