# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish one canonical lab job spec on the contract-declared command topic."""

from __future__ import annotations

import json
import uuid
from importlib import resources
from typing import cast

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.models.lab_job import ModelLabJobSpec
from omnimarket.nodes.node_lab_job_submit_effect.models import ModelLabJobSubmitReceipt


def load_lab_job_submitted_topic() -> str:
    """Read submit_dispatch.command_topic from this node's contract."""
    text = (
        resources.files("omnimarket.nodes.node_lab_job_submit_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    contract = yaml.safe_load(text)
    return cast(str, contract["submit_dispatch"]["command_topic"])


class HandlerLabJobSubmitEffect:
    """Publish a job keyed by its identity; the reducer deduplicates submissions."""

    def __init__(self, bus: ProtocolLabRunBus) -> None:
        self._bus = bus

    async def handle(self, spec: ModelLabJobSpec) -> ModelLabJobSubmitReceipt:
        """Publish the spec envelope and return its publication receipt."""
        topic = load_lab_job_submitted_topic()
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=spec.model_dump(mode="json"),
            correlation_id=uuid.uuid5(uuid.NAMESPACE_URL, spec.job_id),
            event_type=event_type_for(topic),
        )
        await self._bus.publish(
            topic,
            spec.job_id.encode("utf-8"),
            json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
        )
        return ModelLabJobSubmitReceipt(
            status="published", job_id=spec.job_id, topic=topic
        )
