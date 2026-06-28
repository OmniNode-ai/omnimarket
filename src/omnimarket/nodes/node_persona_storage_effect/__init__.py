# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Persona storage effect node — append-only persona snapshot persistence.

Migrated from omnimemory (OMN-8298, Wave 2).
Adapters (Postgres persona) remain in omnimemory and are injected at
runtime via DI. Omnimarket owns the contract, the models, and the
entry point.

Migration status (OMN-8298): DI adapter wiring is incomplete. The
handle() method raises NotImplementedError so the runtime fails loudly
on dispatch rather than silently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from omnibase_core.enums import EnumMessageCategory, EnumNodeKind

from omnimarket.nodes.node_persona_storage_effect.models import (
    ModelPersonaStorageRequest,
    ModelPersonaStorageResponse,
)

if TYPE_CHECKING:
    from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

__all__ = [
    "ModelPersonaStorageRequest",
    "ModelPersonaStorageResponse",
    "NodePersonaStorageEffect",
]


class NodePersonaStorageEffect:
    """ONEX entry-point marker for node_persona_storage_effect.

    Satisfies ProtocolMessageHandler structurally so the runtime discovers
    this class correctly.  The handle() method raises NotImplementedError
    because the omnimemory DI adapter wiring from OMN-8298 is still pending;
    this gives an explicit loud failure on dispatch instead of a silent one.
    """

    __onex_node_type__: str = "node_persona_storage_effect"

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    @property
    def handler_id(self) -> str:
        return "node_persona_storage_effect"

    @property
    def category(self) -> EnumMessageCategory:
        return EnumMessageCategory.COMMAND

    @property
    def message_types(self) -> set[str]:
        return set()

    @property
    def node_kind(self) -> EnumNodeKind:
        return EnumNodeKind.EFFECT

    async def handle(
        self,
        envelope: ModelEventEnvelope[Any],
    ) -> ModelHandlerOutput[Any]:
        """Raise NotImplementedError: adapter wiring pending (OMN-8298).

        The contract handler.class points here, but the omnimemory Postgres
        adapter that performs the actual storage has not been wired via DI.
        Raising here ensures the runtime surfaces a clear error on dispatch
        rather than a silent no-op.
        """
        raise NotImplementedError(  # stub-ok: OMN-8298 Wave-2 DI wiring pending; loud-fail preferred over silent dispatch
            "node_persona_storage_effect.handle() not implemented: "
            "migration pending (OMN-8298) — omnimemory adapter not wired"
        )
