# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20154: the in-process port publishes provider quota observations.

The port observes every metered call its effect makes and delivers the
observation on the topic the provider_quota_state projection subscribes to.
The contract declares that topic; this pins declaration and delivery together.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.events.provider_quota import EnumProviderQuotaSource
from omnimarket.inference.provider_quota_observation import (
    EmitEffectQuotaObservationSink,
    build_quota_observation,
    provider_quota_observed_topic,
)

pytestmark = pytest.mark.unit

_OBSERVED = "onex.evt.omnimarket.provider-quota-observed.v1"  # onex-topic-allow: asserted against the contract
_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_delegate_skill_orchestrator/contract.yaml"
)


def test_the_contract_declares_the_observed_topic() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert _OBSERVED in contract["event_bus"]["publish_topics"]
    assert provider_quota_observed_topic() == _OBSERVED


def test_the_default_sink_publishes_on_the_observed_topic(
    provider_quota_deliveries: list[dict[str, object]],
) -> None:
    observation = build_quota_observation(
        tenant_id=None,
        endpoint_url="https://openrouter.ai/api/v1/chat/completions",
        api_key_ref="llm.openrouter.api_key",
        model_name="qwen/qwen3-coder:free",
        succeeded=True,
        observed_at=datetime.now(UTC),
        latency_ms=5,
        source=EnumProviderQuotaSource.INPROCESS_EFFECT,
    )
    assert observation is not None
    EmitEffectQuotaObservationSink().emit(observation)
    assert [e["topic"] for e in provider_quota_deliveries] == [_OBSERVED]
    assert provider_quota_deliveries[0]["event_id"] == str(observation.event_id)
