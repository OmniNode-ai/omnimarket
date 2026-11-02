# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerClaudeCodeHookAdapter: build, publish, await, verdict (OMN-18531, T0.3).

Generic over bindings. The handler builds the typed command named by the binding
from the hook input, publishes it through the receipt-mode client (which hosts no
handler), awaits the named events, calls the pure verdict compute and returns
client_verdict with the hook's output shape.

A binding whose awaited events hold a `{principal}` slot (OMN-22103) gets the
requester's principal from the injected ``principal_resolver`` (validated against the
contract's principal pattern) before it publishes: the resolved topics are what it
awaits, and an AWAITED_EVENT source argument carries each by position.

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

import re
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from omnimarket.models.claude_code_binding import (
    ModelClientBinding,
)
from omnimarket.models.context_compaction import load_reply_topic_templates
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


def reply_principal_pattern() -> str:
    """The principal pattern the orchestrator contract declares for reply topics."""
    return load_reply_topic_templates().principal_pattern


INVOCATION_ID_SOURCE = "@invocation_id"
INPUT_MESSAGES_HASH_SOURCE = "@input_messages_hash"
SESSION_BARRIER_SOURCE = "@session_barrier"
#: `@awaited_event:<n>` names the binding's n-th awaited event after `{principal}` is
#: substituted, so a command can carry the reply topics it awaits (OMN-22103).
AWAITED_EVENT_SOURCE_PREFIX = "@awaited_event:"
PRINCIPAL_SLOT = "{principal}"
#: The sources a binding argument may name besides a `$.path` into the hook input.
RESERVED_SOURCES = frozenset(
    {INVOCATION_ID_SOURCE, INPUT_MESSAGES_HASH_SOURCE, SESSION_BARRIER_SOURCE}
)


def is_awaited_event_source(source: str) -> bool:
    """True for `@awaited_event:<n>` with a non-negative integer n."""
    index = source.removeprefix(AWAITED_EVENT_SOURCE_PREFIX)
    return source.startswith(AWAITED_EVENT_SOURCE_PREFIX) and index.isdecimal()


def resolve_awaited_events(
    awaited_events: tuple[str, ...], principal: str | None
) -> tuple[str, ...]:
    """Substitute the principal into every `{principal}` slot; none needs no principal."""
    if principal is None:
        return awaited_events
    return tuple(t.replace(PRINCIPAL_SLOT, principal) for t in awaited_events)


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
    awaited_events: tuple[str, ...] | None = None,
) -> dict[str, JsonValue]:
    """Draw fields from hook input, deriving reserved ids, hash, session barrier and reply topics.

    ``awaited_events`` are the binding's awaited events with `{principal}` already
    substituted; a binding whose awaited events still hold the slot cannot name them.
    """
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
    for source in sources:
        if not is_awaited_event_source(source):
            continue
        resolved = binding.awaited_events if awaited_events is None else awaited_events
        index = int(source.removeprefix(AWAITED_EVENT_SOURCE_PREFIX))
        if index >= len(resolved):
            raise ValueError(f"{source} names an awaited event the binding lacks")
        if PRINCIPAL_SLOT in resolved[index]:
            raise ValueError(f"{source} needs the principal substituted first")
        derived[source] = resolved[index]
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
        principal_resolver: Callable[[], str] | None = None,
    ) -> None:
        self._client = client
        self._principal_resolver = principal_resolver
        self._invocation_ids = invocation_ids
        self._barrier_journal = (
            barrier_journal
            if barrier_journal is not None
            else HandlerSessionBarrierJournal()
        )

    def _principal_for(self, binding: ModelClientBinding) -> str | None:
        """The requester's principal when a awaited event is templated on it, else None."""
        if not any(PRINCIPAL_SLOT in t for t in binding.awaited_events):
            return None
        if self._principal_resolver is None:
            raise RuntimeError("no principal resolver is bound to this handler")
        principal = self._principal_resolver()
        if re.fullmatch(reply_principal_pattern(), principal) is None:
            raise ValueError(
                "the resolved principal does not match the principal pattern"
            )
        return principal

    def handle(self, request: ModelHookAdapterRequest) -> ModelHookAdapterResult:
        if self._client is None:
            raise RuntimeError("no receipt-mode client is bound to this handler")
        invocation_id = self._invocation_ids()
        binding = request.binding
        awaited_events = resolve_awaited_events(
            binding.awaited_events, self._principal_for(binding)
        )
        events = self._client.publish_and_await(
            topic=binding.command_topic,
            command_type=binding.command_type,
            payload=build_command_payload(
                binding,
                request.hook_input,
                invocation_id=invocation_id,
                barrier_journal=self._barrier_journal,
                awaited_events=awaited_events,
            ),
            awaited_events=awaited_events,
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
