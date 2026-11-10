# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the example envelope from settings and an injected identity source."""

from __future__ import annotations

from omnimarket.models.model_envelope_example import (
    ModelEnvelopeExampleRequest,
    ModelEnvelopeExampleSettings,
)
from omnimarket.models.model_onex_envelope import (
    ModelEnvelopeMetadata,
    ModelOnexEnvelope,
)
from omnimarket.protocols.protocol_envelope_example_source import (
    ProtocolEnvelopeIdentitySource,
)


class EnvelopeExampleBuilder:
    """Stateless: the same settings and source give one fresh example per call."""

    def __init__(
        self,
        settings: ModelEnvelopeExampleSettings,
        source: ProtocolEnvelopeIdentitySource,
    ) -> None:
        self._settings = settings
        self._source = source

    def handle(self, request: ModelEnvelopeExampleRequest) -> ModelOnexEnvelope:
        settings = self._settings
        payload: dict[str, str] = dict(settings.payload)
        for name in settings.generated_id_fields:
            payload[name] = str(self._source.new_id())
        return ModelOnexEnvelope(
            envelope_id=self._source.new_id(),
            envelope_version=settings.envelope_version,
            correlation_id=self._source.new_id(),
            source_node=settings.source_node,
            operation=settings.operation,
            timestamp=self._source.now(),
            metadata=ModelEnvelopeMetadata(tags=dict(settings.tags)),
            payload=dict(payload),
        )


__all__ = ["EnvelopeExampleBuilder"]
