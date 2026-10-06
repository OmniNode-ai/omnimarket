# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish one canonical lab job spec on the contract-declared command topic."""

from __future__ import annotations

import uuid
from importlib import resources
from typing import cast

import yaml

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolLabRunBus,
    envelope_bytes_for,
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
        await self._bus.publish(
            topic,
            spec.job_id.encode("utf-8"),
            envelope_bytes_for(
                topic,
                spec.model_dump(mode="json"),
                uuid.uuid5(uuid.NAMESPACE_URL, spec.job_id),
            ),
        )
        return ModelLabJobSubmitReceipt(
            status="published", job_id=spec.job_id, topic=topic
        )
