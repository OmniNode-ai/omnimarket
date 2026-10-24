# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerClaudeCodeHookAdapter: build, publish, await, verdict (OMN-18531, T0.3).

Generic over bindings. The handler builds the typed command named by the binding
from the hook input, publishes it through the receipt-mode client (which hosts no
handler), awaits the named events, calls the pure verdict compute and returns
client_verdict with the hook's output shape.

The adapter mints a fresh uuid4 invocation_id for every invocation (plan section 6:
carried on the command, the package reply and any fallback). An argument whose
source is INVOCATION_ID_SOURCE carries it on the command. An argument whose
source is INPUT_MESSAGES_HASH_SOURCE carries the hash of the conversation being
compacted, derived from the hook input's messages or transcript (OMN-21099).
SESSION_BARRIER_SOURCE journals the session's capture high-waters before the
command is published and carries their shared barrier id (TA.21); every other
field is drawn from the hook input by its path.
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
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_input_messages_hash import (
    input_messages_hash,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_session_barrier_journal import (
    HandlerSessionBarrierJournal,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.models import (
    ModelHookAdapterRequest,
    ModelHookAdapterResult,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.protocols import (
    ProtocolReceiptModeClient,
    ProtocolSessionBarrierJournal,
)

if TYPE_CHECKING:
    from pydantic import JsonValue


INVOCATION_ID_SOURCE = "@invocation_id"
INPUT_MESSAGES_HASH_SOURCE = "@input_messages_hash"
SESSION_BARRIER_SOURCE = "@session_barrier"
#: The sources a binding argument may name besides a `$.path` into the hook input.
RESERVED_SOURCES = frozenset(
    {INVOCATION_ID_SOURCE, INPUT_MESSAGES_HASH_SOURCE, SESSION_BARRIER_SOURCE}
)


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
    barrier_journal: ProtocolSessionBarrierJournal | None = None,
) -> dict[str, JsonValue]:
    """Draw fields from hook input, deriving reserved ids, hash and session barrier."""
    sources = {a.source for a in binding.arguments}
    derived: dict[str, JsonValue] = {}
    if invocation_id is not None:
        derived[INVOCATION_ID_SOURCE] = str(invocation_id)
    if INPUT_MESSAGES_HASH_SOURCE in sources:
        derived[INPUT_MESSAGES_HASH_SOURCE] = input_messages_hash(hook_input)
    if SESSION_BARRIER_SOURCE in sources:
        session_id = resolve_source(hook_input, "$.session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session barrier needs a non-empty session_id")
        if barrier_journal is None:
            raise ValueError("session barrier needs a barrier_journal")
        derived[SESSION_BARRIER_SOURCE] = barrier_journal.journal(session_id)
    return {
        a.name: derived[a.source]
        if a.source in derived
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
        barrier_journal: ProtocolSessionBarrierJournal | None = None,
    ) -> None:
        self._client = client
        self._invocation_ids = invocation_ids
        self._barrier_journal = (
            barrier_journal
            if barrier_journal is not None
            else HandlerSessionBarrierJournal()
        )

    def handle(self, request: ModelHookAdapterRequest) -> ModelHookAdapterResult:
        if self._client is None:
            raise RuntimeError("no receipt-mode client is bound to this handler")
        invocation_id = self._invocation_ids()
        binding = request.binding
        events = self._client.publish_and_await(
            topic=binding.command_topic,
            command_type=binding.command_type,
            payload=build_command_payload(
                binding,
                request.hook_input,
                invocation_id=invocation_id,
                barrier_journal=self._barrier_journal,
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
