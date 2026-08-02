# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-facing delegation request model (shared delegation wire contract).

Distinct from the runtime-internal ``ModelDelegationRequest``: consumers supply
``source``, ``cwd``, ``wait``, and ``metadata`` and never set the runtime-internal
``emitted_at`` / ``output_schema_key`` / ``compliance_budget``. The ``task_type``
Literal is the MVP taxonomy and must match the delegate node contract.yaml
``allowed_task_types`` field.

This lives in the shared delegation wire package (alongside
``ModelDelegateSkillResponse``) so any node composing the delegation route can
reference it without reaching into a sibling node's private models package
(OMN-12704). ``node_delegate_skill_orchestrator`` re-exports it for compatibility.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.events.delegation import (
    EnumQualityContractMode,
    validate_acceptance_criteria,
)

# OMN-15482: the closed set of ``response_format`` directives this delegation
# path genuinely threads end-to-end (onto the outbound chat-completions
# payload). Deliberately narrow: OpenAI's ``json_schema`` structured-output
# mode is NOT accepted, because the local dispatch path does not translate it
# and a silently-ignored directive is the exact fidelity defect this ticket
# closes. Schema-level response constraints belong on ``response_contract``.
_SUPPORTED_RESPONSE_FORMAT_TYPES: frozenset[str] = frozenset({"json_object"})


class ModelDelegateSkillRequest(BaseModel):
    """Typed delegation request from a registered adapter source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(..., min_length=1, description="User prompt to delegate.")
    task_type: Literal[
        "test",
        "document",
        "research",
        "code_generation",
        "code_review",
        "refactor",
        "reasoning",
        "complex_reasoning",
        "planning",
        "review",
        "summarization",
        "agent_delegation",
        "escalation",
        "documentation",
        "validator_generation",
    ] = Field(
        ...,
        description=(
            "Task classification for routing. Must match contract allowed_task_types."
        ),
    )
    source: Literal["claude-code", "codex", "external-client"] = Field(
        ...,
        description="Registered adapter source.",
    )
    cwd: str | None = Field(default=None, description="Caller current directory.")
    source_file_path: str | None = Field(
        default=None,
        description="File context for the delegation, if any.",
    )
    working_directory: str | None = Field(
        default=None,
        description="Worker working directory requested by the caller.",
    )
    session_id: str | None = Field(
        default=None,
        description="Session that originated the delegation request.",
    )
    recipient: str | None = Field(
        default=None,
        description="Requested delegation recipient surface.",
    )
    codex_sandbox_mode: str | None = Field(
        default=None,
        description="Codex sandbox mode requested by the caller.",
    )
    wait: bool = Field(default=True, description="Wait for synchronous result.")
    max_tokens: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional explicit output-token budget. When omitted (None) the "
            "orchestrator resolves the effective value from the selected backend's "
            "per-backend ceiling in the routing contract; when supplied it is "
            "capped at that backend ceiling (OMN-13161)."
        ),
    )
    correlation_id: UUID = Field(default_factory=uuid4)
    metadata: dict[str, str] = Field(default_factory=dict)
    quality_contract_mode: EnumQualityContractMode = Field(
        default="extend_task_class",
        description=(
            "How request-level acceptance criteria interact with task-class DoD."
        ),
    )
    acceptance_criteria: tuple[str, ...] = Field(
        default=(),
        description=(
            "Request-level quality criteria validated before dispatch and enforced "
            "by the delegation quality gate."
        ),
    )
    # string-id-ok: tenant_id is a named tenant identifier (slug), not a UUID.
    # Mirrors the field already shipped on omnibase_core's ModelDelegationRequest
    # (OMN-14058). None on construction means no verified identity was stamped
    # upstream -- OMN-14208 Path A's tenant-ingress node stamps a real value into
    # the raw payload (topic-prefix-derived) before this model validates, on the
    # bus path; the bus-less local CLI path leaves this None and falls back to
    # the ONEX_TENANT_ID interim (OMN-14058) further downstream.
    tenant_id: str | None = Field(
        default=None,
        description=(
            "Multi-tenant isolation identifier, verified upstream when present. "
            "Never a self-reported/client-writable value."
        ),
    )
    # OMN-15180: optional caller-supplied backend PIN. None (the default)
    # preserves the exact pre-existing cheapest-first task_type + tier_order
    # resolution. A non-None value is threaded verbatim to
    # HandlerDelegateSkill.handle() -> dispatch_port.dispatch(backend_id=...),
    # reusing the OMN-15156 pin LocalDelegationDispatchPort already implements
    # (resolve_delegation_backend(task_type, backend_id=...), bypassing tier
    # selection for the INITIAL attempt only). This is what makes a wire-level
    # caller (e.g. steel's LlmBusDelegationClient, OMN-15159) able to reach a
    # specific backend such as local-coder-mlx deterministically.
    backend_id: str | None = Field(
        default=None,
        description=(
            "Optional explicit backend pin (e.g. 'local-coder-mlx'). None resolves "
            "the backend via the normal cheapest-first tier_order selection."
        ),
    )
    # OMN-15193: optional caller-declared JSON-Schema response contract. None
    # (the default) is threaded to dispatch_port.dispatch(response_contract=None),
    # which then resolves the TASK-CLASS-DECLARED default schema, if any
    # (`response_contract_ref` in task_class_contracts.v1.yaml, OMN-15196) --
    # for `agent_delegation` this is the per-role dispatch report contract
    # (OMN-15161), which REPLACES the now-retired `sub_tasks_verified`/
    # `no_refusal` keyword heuristics. A non-None value here is threaded
    # verbatim to HandlerDelegateSkill.handle() -> dispatch_port.dispatch(
    # response_contract=...) -> the quality-gate reducer (`delta`), and always
    # takes precedence over the task-class default -- structural JSON-Schema
    # validation against THIS schema applies for THIS request only, without
    # touching the task-class contract any other caller of the same task_type
    # still sees. This is what makes a caller that knows its own response shape
    # (e.g. steel's LlmBusDelegationClient, OMN-15170) immune to a
    # false-positive refusal match on a legitimate rationale substring like
    # "i cannot", even for a task class with no declared default (or one whose
    # default does not match this caller's own response shape).
    response_contract: dict[str, object] | None = Field(
        default=None,
        description=(
            "Optional JSON-Schema-shaped contract describing the expected "
            "response structure. When set, the quality gate validates the "
            "response structurally against this schema instead of the "
            "task-class keyword heuristics. None falls back to the task-class "
            "declared default schema (if any), then to the legacy heuristics."
        ),
    )
    # OMN-15482: the three completion-shaping parameters below close the
    # measured fidelity gap between this delegation wire model and a direct
    # OpenAI-compatible chat-completions call. Before this ticket a consumer
    # that had a system prompt, a temperature, or a JSON-mode requirement could
    # not express any of them here, so a client migrating from a direct HTTP
    # provider binding onto the delegation path silently changed behaviour --
    # temperature was dropped, the system/user role split collapsed into one
    # concatenated ``prompt`` string, and JSON mode degraded into an appended
    # prompt sentence. Every field defaults to ``None``, and ``None`` preserves
    # the exact pre-existing behaviour on every path, so no existing caller
    # changes shape.
    system_prompt: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Optional caller-supplied system message, sent as a distinct "
            "``role: system`` chat message alongside ``prompt`` (``role: "
            "user``). None resolves the task-type default system prompt the "
            "routing layer already applies -- the pre-existing behaviour."
        ),
    )
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description=(
            "Optional sampling temperature forwarded verbatim to the provider. "
            "None resolves the effect-layer default, preserving pre-existing "
            "behaviour for every caller that does not set it."
        ),
    )
    response_format: dict[str, object] | None = Field(
        default=None,
        description=(
            "Optional OpenAI-shaped response-format directive, forwarded "
            "verbatim as a wire parameter on the outbound chat-completions "
            "payload. Only ``{'type': 'json_object'}`` is supported; anything "
            "else is rejected rather than silently dropped. None omits the "
            "parameter entirely (pre-existing behaviour). Distinct from "
            "``response_contract``, which drives ONEX-side structural "
            "validation of the response and is never sent to the provider."
        ),
    )

    @field_validator("acceptance_criteria")
    @classmethod
    def _validate_supported_acceptance_criteria(
        cls, criteria: tuple[str, ...]
    ) -> tuple[str, ...]:
        return validate_acceptance_criteria(criteria)

    @field_validator("response_format")
    @classmethod
    def _validate_supported_response_format(
        cls, response_format: dict[str, object] | None
    ) -> dict[str, object] | None:
        """Reject any response-format directive this path does not really thread.

        Fail-loud, not permissive: accepting an unsupported directive (e.g.
        OpenAI's ``json_schema`` structured-output mode) and forwarding it to a
        provider that ignores it would reintroduce exactly the silent-fidelity
        class OMN-15482 exists to close -- the caller would believe it had
        constrained the response when it had not. ``response_contract`` is the
        ONEX-native surface for schema-level response constraints.
        """
        if response_format is None:
            return None
        if set(response_format) != {"type"}:
            raise ValueError(
                "response_format must contain exactly the key 'type'; got "
                f"{sorted(response_format)!r}"
            )
        if response_format["type"] not in _SUPPORTED_RESPONSE_FORMAT_TYPES:
            raise ValueError(
                "unsupported response_format type "
                f"{response_format['type']!r}; supported: "
                f"{sorted(_SUPPORTED_RESPONSE_FORMAT_TYPES)!r}. Use "
                "response_contract for schema-level response validation."
            )
        return response_format


__all__: list[str] = [
    "EnumQualityContractMode",
    "ModelDelegateSkillRequest",
]
