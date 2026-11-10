# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed handler of GET /v1/envelopes/example: request in, example envelope out."""

from __future__ import annotations

from pathlib import Path

from omnimarket.handlers.envelope_example.builder import EnvelopeExampleBuilder
from omnimarket.handlers.envelope_example.defaults import default_builder
from omnimarket.models.model_envelope_example import ModelEnvelopeExampleRequest
from omnimarket.models.model_onex_envelope import ModelOnexEnvelope

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


class HandlerEnvelopeExampleGet:
    def __init__(self, builder: EnvelopeExampleBuilder | None = None) -> None:
        # Built bare by the runtime, the handler takes its settings from the
        # contract, the installed lane overlay and the system identity source.
        self._builder = builder or default_builder(CONTRACT_PATH)

    def handle(self, request: ModelEnvelopeExampleRequest) -> ModelOnexEnvelope:
        return self._builder.handle(request)
