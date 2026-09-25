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

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from omnibase_core.models.delegation.wire import ModelDelegationProvenance
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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

# OMN-18931: the dogfood fault-route policy key omnibase_infra's delegation
# dispatch port publishes beside a pinned ``backend_id`` and an exact
# ``requested_timeout_seconds``. This model does not declare it yet; see
# ``_tolerate_no_escalation_before_it_is_declared`` for why and for the order
# in which it becomes a field.
NO_ESCALATION_WIRE_KEY = "no_escalation"


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
    provenance: ModelDelegationProvenance | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Typed ingress provenance carried unchanged through delegation state "
            "and terminal evidence. None is legacy/unclassified, never synthetic."
        ),
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
    # OMN-19131: `exclude_if` here is the same load-bearing rollout guard the
    # `published_at` block below spells out, applied to the second field that
    # needed it. Without it this serialises as `"requested_timeout_seconds":
    # null` on EVERY delegation -- including the overwhelming majority that
    # never passed `--timeout` -- and `RuntimeLocal` publishes the record as
    # `model_dump_json()`, so the null reaches every consumer. A consumer
    # baked before this field declares no such field and `extra="forbid"`, so
    # it refuses the record as publisher-malformed.
    #
    # Measured on the lab 2026-09-22, by importing each container's OWN copy of
    # this model rather than comparing version strings (both sides report the
    # same version, which is why OMN-18852 went undetected): the `.201`
    # `omnibase-infra` dev lane's runtime pair carries the field, while the
    # `omnibase-infra-stability-test` lane's pair does not and forbids extras.
    # One producer, one record, accepted on one lane and refused on the other.
    requested_timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
        description=(
            "Requested handler execution timeout. None means the task-class "
            "execution ceiling is the effective timeout, and is omitted from "
            "serialisation entirely so a consumer predating this field is not "
            "handed an extra key it forbids."
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

    # OMN-18852: the instant the command record was PUBLISHED, stamped by the
    # producer. It is the only thing that makes queue wait -- publish to
    # handler pickup -- measurable at the handler, because a definition-B
    # handler receives the typed request and never the Kafka record. Measured
    # on the .201 dev lane 2026-09-19: queue wait grew from 3 s to 445 s across
    # nine delegations in 26 minutes, and a control run spent 179 s of its
    # 181 s wall clock queued. None means the producer did not stamp it, so the
    # terminal reports queue wait as NOT MEASURED rather than as zero -- an
    # unstamped request and an empty queue must not read the same.
    # OMN-18852 ROLLOUT FIX: `exclude_if` is load-bearing, not tidiness.
    #
    # Without it this field serialises as `"published_at": null` on every
    # request, and a consumer that has not yet been rebuilt with this model
    # declares `extra="forbid"` and no such field -- so it refuses the record
    # as publisher-malformed. That is not hypothetical: it is what happened
    # between 2026-09-19T22:10:50Z and this fix. The dispatch venv reconciled
    # to the producing commit 16 s after merge, while the lane's consumer was
    # baked into a 21:06Z image, so EVERY wrapper `onex delegate` dead-lettered
    # 6 s in with "published_at: Extra inputs are not permitted" (offset 725).
    # Both sides reported version 0.4.134, so a version comparison could not
    # detect it and did not.
    #
    # THE RULE THIS ENCODES: a new optional field on a wire contract whose
    # consumers forbid extras must either land consumer-first, or be excluded
    # when unset. Adding a field to a shared model is a rollout event, not a
    # local edit -- producer and consumer advance independently and the
    # producer here advances FASTER, because a venv reconcile is seconds and an
    # image rebuild is minutes to hours.
    #
    # Once a producer actually stamps a value the field serialises normally,
    # and by then every consumer carrying this model knows the field.
    published_at: datetime | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Timezone-aware instant the command record was published. None "
            "means the producer did not stamp it and queue wait is not "
            "measurable for this request. Omitted from serialisation entirely "
            "when None, so a consumer predating this field is not handed an "
            "extra key it forbids."
        ),
    )

    # OMN-18931, step 1 of 2: a CONSUMER that decodes ``no_escalation`` before
    # any producer on this package can emit it.
    #
    # Declaring the field outright is the OMN-18852 class, and the OMN-18868
    # Wire Compatibility Gate refuses it: the gate replays the MAXIMAL key set
    # through the last released model, that model forbids extras, and
    # ``exclude_if`` does not help because the gate reads ``model_fields``.
    # The gate's own remedy is the release order. So this release decodes the
    # key without declaring it, and step 2 -- the declared field, emitted only
    # when true -- passes the gate once a release carrying this is out.
    #
    # Tolerating is not dropping everything. ``false`` and ``null`` are the
    # ordinary route and are dropped. ``true`` asks for a one-hop, no-retry,
    # no-escalation policy that nothing in this release enforces, and dropping
    # it would quietly turn a fault control into an ordinary escalating
    # delegation. So ``true`` is refused by name. The gate grades only
    # ``extra_forbidden`` and ``missing``, so a named refusal still reads as a
    # consumer that decodes the key.
    #
    # Step 2 deletes this validator in the same change that declares the field.
    @model_validator(mode="before")
    @classmethod
    def _tolerate_no_escalation_before_it_is_declared(cls, data: Any) -> Any:
        if not isinstance(data, Mapping) or NO_ESCALATION_WIRE_KEY not in data:
            return data
        value = data[NO_ESCALATION_WIRE_KEY]
        if value is not None and value is not False:
            raise ValueError(
                f"{NO_ESCALATION_WIRE_KEY}={value!r} is not honoured by this "
                "release: the no-escalation fault-route policy needs a consumer "
                "that declares the field (OMN-18931 step 2). Refused rather "
                "than dropped, so the request cannot escalate unseen."
            )
        return {
            key: item for key, item in data.items() if key != NO_ESCALATION_WIRE_KEY
        }

    @field_validator("published_at")
    @classmethod
    def _require_timezone_aware_published_at(
        cls, published_at: datetime | None
    ) -> datetime | None:
        """Refuse a naive timestamp rather than guessing a zone for it.

        Producer and consumer are separate processes and need not share a
        local zone. Assuming one would silently produce a queue wait wrong by
        hours, and a wrong measurement is worse than an absent one.
        """
        if published_at is not None and published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        return published_at

    @field_validator("acceptance_criteria")
    @classmethod
    def _validate_supported_acceptance_criteria(
        cls, criteria: tuple[str, ...]
    ) -> tuple[str, ...]:
        return validate_acceptance_criteria(criteria)

    @model_validator(mode="after")
    def _provenance_source_matches_adapter(self) -> ModelDelegateSkillRequest:
        """Keep the two registered-source fields from contradicting each other."""
        if self.provenance is not None and self.provenance.source != self.source:
            raise ValueError("provenance.source must match source")
        return self

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
    "NO_ESCALATION_WIRE_KEY",
    "EnumQualityContractMode",
    "ModelDelegateSkillRequest",
]
