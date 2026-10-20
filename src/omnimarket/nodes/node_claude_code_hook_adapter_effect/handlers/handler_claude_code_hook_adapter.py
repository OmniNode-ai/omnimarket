# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerClaudeCodeHookAdapter: build, publish, await, verdict (OMN-18531, T0.3).

Generic over bindings. The handler builds the typed command named by the binding
from the hook input, publishes it through the receipt-mode client (which hosts no
handler), awaits the named events, calls the pure verdict compute and returns
client_verdict with the hook's output shape.

The adapter mints a fresh uuid4 invocation_id for every invocation (plan section 6:
carried on the command, the package reply and any fallback). An argument whose
source is INVOCATION_ID_SOURCE carries it on the command; every other field is
still drawn from the hook input.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from omnimarket.models.claude_code_binding import (
    ModelClientBinding,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_client_verdict import (
    compute_client_verdict,
    hook_output_for,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_compaction_verdict import (
    COMPACTION_VERDICT_COMPUTE,
    compute_compaction_verdict,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.models import (
    ModelHookAdapterRequest,
    ModelHookAdapterResult,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.protocols import (
    ProtocolReceiptModeClient,
)

if TYPE_CHECKING:
    from pydantic import JsonValue


INVOCATION_ID_SOURCE = "@invocation_id"


def resolve_source(hook_input: dict[str, JsonValue], source: str) -> JsonValue:
    """Walk a `$.a.b` path into the hook input; an absent step is null."""
    node: JsonValue = hook_input
    for step in source.removeprefix("$").strip(".").split("."):
        if not isinstance(node, dict) or step not in node:
            return None
        node = node[step]
    return node


def build_command_payload(
    binding: ModelClientBinding,
    hook_input: dict[str, JsonValue],
    *,
    invocation_id: UUID | None = None,
) -> dict[str, JsonValue]:
    """Draw fields from hook input, with the reserved source carrying the minted id."""
    return {
        a.name: str(invocation_id)
        if a.source == INVOCATION_ID_SOURCE and invocation_id is not None
        else resolve_source(hook_input, a.source)
        for a in binding.arguments
    }


class HandlerClaudeCodeHookAdapter:
    """handle(request) -> result; the client and invocation id factory are injectable.

    The client is optional only so runtime auto-wiring can construct the handler; the
    console entry always passes one, and handle refuses to run without it.
    """

    def __init__(
        self,
        client: ProtocolReceiptModeClient | None = None,
        *,
        invocation_ids: Callable[[], UUID] = uuid4,
    ) -> None:
        self._client = client
        self._invocation_ids = invocation_ids

    def handle(self, request: ModelHookAdapterRequest) -> ModelHookAdapterResult:
        if self._client is None:
            raise RuntimeError("no receipt-mode client is bound to this handler")
        invocation_id = self._invocation_ids()
        binding = request.binding
        events = self._client.publish_and_await(
            topic=binding.command_topic,
            command_type=binding.command_type,
            payload=build_command_payload(
                binding, request.hook_input, invocation_id=invocation_id
            ),
            awaited_events=binding.awaited_events,
            deadline_ms=binding.deadline_ms,
        )
        verdict = (
            compute_compaction_verdict(binding, events, invocation_id)
            if binding.verdict_compute == COMPACTION_VERDICT_COMPUTE
            else compute_client_verdict(binding, events)
        )
        return ModelHookAdapterResult(
            client_verdict=verdict,
            hook_output=hook_output_for(binding, verdict),
            invocation_id=invocation_id,
        )
