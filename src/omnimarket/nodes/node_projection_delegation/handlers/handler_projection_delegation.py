"""HandlerProjectionDelegation — project task-delegated events to DB.

Consumes onex.evt.omniclaude.task-delegated.v1 and UPSERTs into
the delegation_events table. Dedup by correlation_id.

Target table schema (from omnidash, OMN-2284):
  id UUID PRIMARY KEY DEFAULT gen_random_uuid()
  correlation_id TEXT UNIQUE NOT NULL
  session_id TEXT
  timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
  task_type TEXT NOT NULL
  delegated_to TEXT NOT NULL
  model_name TEXT DEFAULT ''
  delegated_by TEXT
  quality_gate_passed BOOLEAN DEFAULT false
  quality_gates_checked INT
  quality_gates_failed INT
  quality_gates_checked_jsonb JSONB
  quality_gates_failed_jsonb JSONB
  cost_usd NUMERIC DEFAULT 0
  cost_savings_usd NUMERIC DEFAULT 0
  delegation_latency_ms INT
  repo TEXT
  is_shadow BOOLEAN DEFAULT false
  llm_call_id TEXT
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.topics import (
    DELEGATE_SKILL_COMPLETED_TOPIC_V1,
    DELEGATE_SKILL_FAILED_TOPIC_V1,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
    ModelDelegationEventProjectionRow,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "delegation_events"
CONFLICT_KEY = "correlation_id"
GENERATION_TABLE = "generation_events"

# OMN-12775 (close-the-loop A3): canonical owner of the generation_events
# projection — the node that writes the row. Persisted so the dashboard renders
# the real owner instead of its reader-side fallback string.
GENERATION_PROJECTION_OWNER = "node_projection_delegation"


def compute_generation_proof_fields(
    *,
    contract_yaml: str,
    handler_source: str,
    routing_source: str,
    resolved_endpoint: str,
) -> dict[str, str]:
    """Build the six generation_events proof fields (OMN-12775, A3).

    The SHA256 fields are deterministic digests of the FULL stored payload, so a
    verifier can recompute them from the persisted contract_yaml/handler_source
    and prove no truncation occurred. routing_source and resolved_endpoint are
    carried verbatim from the routing authority. projection_owner is the
    canonical node that writes the row. Single source of truth shared by both the
    sync (live-runtime) and async (runner) write paths so they cannot drift.
    """
    contract_sha256 = hashlib.sha256(contract_yaml.encode()).hexdigest()
    handler_sha256 = hashlib.sha256(handler_source.encode()).hexdigest()
    output_payload_sha256 = hashlib.sha256(
        (contract_yaml + handler_source).encode()
    ).hexdigest()
    return {
        "output_payload_sha256": output_payload_sha256,
        "contract_sha256": contract_sha256,
        "handler_sha256": handler_sha256,
        "routing_source": routing_source,
        "resolved_endpoint": resolved_endpoint,
        "projection_owner": GENERATION_PROJECTION_OWNER,
    }


class ModelProjectionGenerationCompletedEvent(BaseModel):
    """Inbound event from onex.evt.omnimarket.node-generation-completed.v1.

    The live runtime dispatches this through HandlerProjectionDelegation.handle()
    (the contract `handler:` field); the *ProjectionRunner sibling is skipped by
    the DB-injection auto-wiring path (OMN-12800).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(..., description="Unique correlation ID for dedup.")
    task_description: str = Field(default="")
    provider: str = Field(default="")
    model_id: str = Field(default="")
    endpoint_class: str = Field(default="")
    attempt_count: int = Field(default=0, ge=0)
    total_latency_e2e_ms: int = Field(default=0, ge=0)
    contract_passed: bool = Field(default=False)
    cost_inference_usd: float = Field(default=0.0)
    timestamp: str | None = Field(default=None, description="ISO 8601 timestamp.")
    # OMN-12780 (Wave 1C): full generated output — empty string is the
    # failed/incomplete-generation sentinel, never coerced to NULL, never truncated.
    contract_yaml: str = Field(default="")
    handler_source: str = Field(default="")
    # OMN-12775 (close-the-loop A3): routing-authority proof carried verbatim from
    # the generation terminal event. The SHA256 proof fields are derived in the
    # write path from the full payload (the projection can recompute them), but
    # the routing decision must be carried — the projection cannot reconstruct it.
    routing_source: str = Field(default="")
    resolved_endpoint: str = Field(default="")


class ModelProjectionTaskDelegatedEvent(BaseModel):
    """Inbound event from onex.evt.omniclaude.task-delegated.v1."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(..., description="Unique correlation ID for dedup.")
    session_id: str | None = Field(default=None)
    task_type: str = Field(..., description="Task type (e.g. code-review, refactor).")
    delegated_to: str = Field(..., description="Agent that received the task.")
    model_name: str = Field(
        default="", description="LLM model name used for inference."
    )
    delegated_by: str | None = Field(default=None)
    quality_gate_passed: bool = Field(default=False)
    quality_gates_checked: list[str] | None = Field(default=None)
    quality_gates_failed: list[str] | None = Field(default=None)
    quality_gate_detail: str | None = Field(default=None)
    cost_usd: float = Field(default=0.0)
    cost_savings_usd: float = Field(default=0.0)
    delegation_latency_ms: int | None = Field(default=None, ge=0)
    repo: str | None = Field(default=None)
    is_shadow: bool = Field(default=False)
    llm_call_id: str = Field(
        default="",
        description="Upstream LLM call ID for JOIN with llm_cost_aggregates.",
    )
    timestamp: str | None = Field(default=None, description="ISO 8601 timestamp.")
    tokens_input: int = Field(default=0, ge=0)
    tokens_output: int = Field(default=0, ge=0)
    tokens_to_compliance: int = Field(
        default=0,
        ge=0,
        description=(
            "Total tokens consumed across all schema-compliance attempts (OMN-10793)."
        ),
    )
    compliance_attempts: int = Field(
        default=1,
        ge=1,
        description=(
            "Number of LLM invocations needed to produce contract-compliant output "
            "(OMN-10793). 1 = first-try success."
        ),
    )
    prompt_text: str | None = Field(
        default=None,
        description="Raw prompt sent to the delegated agent (OMN-10850).",
    )
    response_text: str | None = Field(
        default=None,
        description="Raw response received from the delegated agent (OMN-10850).",
    )
    pricing_manifest_version: int = Field(
        default=0,
        ge=0,
        description="Version of the pricing manifest used to compute cost_savings_usd (OMN-10949).",
    )


class ModelProjectionResult(BaseModel):
    """Result of a projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


ModelTaskDelegatedEvent = ModelProjectionTaskDelegatedEvent


class HandlerProjectionDelegation:
    """Project task-delegated events into delegation_events table."""

    _delegate_skill_terminal_events = frozenset(
        {
            "delegate-skill-completed",
            "delegate-skill-failed",
            DELEGATE_SKILL_COMPLETED_TOPIC_V1,
            DELEGATE_SKILL_FAILED_TOPIC_V1,
        }
    )

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to project() with a ModelTaskDelegatedEvent and
        a DatabaseAdapter from input_data['_db'].
        """
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event_type = str(payload.pop("_event_type", ""))
        if "node-generation-completed" in event_type:
            generation = ModelProjectionGenerationCompletedEvent(**payload)
            result = self.project_generation_completed(generation, db_raw)
            return result.model_dump(mode="json")
        if (
            event_type in self._delegate_skill_terminal_events
            or _is_delegate_skill_terminal_payload(payload)
        ):
            terminal = ModelDelegateSkillTerminalProjection.from_payload(payload)
            result = self.project_delegate_skill_terminal(terminal, db_raw)
            return result.model_dump(mode="json")
        if "delegation-completed" in event_type or "delegation-failed" in event_type:
            payload = _canonical_result_to_task_delegated_payload(payload)

        event = ModelTaskDelegatedEvent(**payload)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelTaskDelegatedEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a single delegation event."""
        now = datetime.now(tz=UTC).isoformat()
        row: dict[str, object] = {
            "correlation_id": event.correlation_id,
            "session_id": event.session_id,
            "timestamp": event.timestamp or now,
            "task_type": event.task_type,
            "delegated_to": event.delegated_to,
            "model_name": event.model_name,
            "delegated_by": event.delegated_by,
            "quality_gate_passed": event.quality_gate_passed,
            "quality_gates_checked": _gate_count(event.quality_gates_checked),
            "quality_gates_failed": _gate_count(event.quality_gates_failed),
            "quality_gates_checked_jsonb": event.quality_gates_checked,
            "quality_gates_failed_jsonb": event.quality_gates_failed,
            "quality_gate_detail": event.quality_gate_detail,
            "cost_usd": event.cost_usd,
            "cost_savings_usd": event.cost_savings_usd,
            "delegation_latency_ms": event.delegation_latency_ms,
            "repo": event.repo,
            "is_shadow": event.is_shadow,
            "llm_call_id": event.llm_call_id or None,
            "tokens_input": event.tokens_input,
            "tokens_output": event.tokens_output,
            "tokens_to_compliance": event.tokens_to_compliance,
            "compliance_attempts": event.compliance_attempts,
            "prompt_text": event.prompt_text,
            "response_text": event.response_text,
            "pricing_manifest_version": event.pricing_manifest_version,
        }
        _preserve_existing_evidence(db, row)
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_delegate_skill_terminal(
        self,
        event: ModelDelegateSkillTerminalProjection,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a typed delegate-skill terminal event into delegation_events.

        OMN-13121: a well-formed terminal event is always upserted, even when
        tokens_input, tokens_output and cost_usd are all zero. Zero-token/zero-cost
        is the steady state for free local-LLM delegations and golden-chain proofs,
        not a malformed event — the prior OMN-11923 guard silently dropped these,
        stranding the organic delegation tail at zero rows. Genuinely malformed or
        empty payloads cannot reach this method: ModelDelegateSkillTerminalProjection
        requires status, correlation_id and task_type, so an empty payload
        raises a validation error in from_payload() rather than being silently
        dropped here. Dedup against synthetic re-emits is handled by the
        correlation_id UPSERT key plus _preserve_existing_evidence.
        """
        row_model = ModelDelegationEventProjectionRow.from_terminal_event(event)
        row: dict[str, object] = {
            "correlation_id": str(row_model.correlation_id),
            "session_id": (
                str(row_model.session_id) if row_model.session_id is not None else None
            ),
            "timestamp": row_model.timestamp.isoformat(),
            "task_type": row_model.task_type,
            "delegated_to": row_model.delegated_to,
            "model_name": row_model.model_name,
            "delegated_by": row_model.delegated_by,
            "quality_gate_passed": row_model.quality_gate_passed,
            "quality_gates_checked": len(row_model.quality_gates_checked),
            "quality_gates_failed": len(row_model.quality_gates_failed),
            "quality_gates_checked_jsonb": list(row_model.quality_gates_checked),
            "quality_gates_failed_jsonb": list(row_model.quality_gates_failed),
            "quality_gate_detail": row_model.quality_gate_detail,
            "cost_usd": row_model.cost_usd,
            "cost_savings_usd": row_model.cost_savings_usd,
            "delegation_latency_ms": row_model.latency_ms,
            "latency_ms": row_model.latency_ms,
            "repo": row_model.repo_name,
            "is_shadow": row_model.is_shadow,
            "prompt_text": row_model.prompt_text,
            "response_text": row_model.response_text,
            "tokens_input": row_model.tokens_input,
            "tokens_output": row_model.tokens_output,
            "tokens_to_compliance": row_model.tokens_to_compliance,
            "compliance_attempts": row_model.compliance_attempts,
            "pricing_manifest_version": row_model.pricing_manifest_version,
            "projection_version": row_model.projection_version,
            "reducer_version": row_model.reducer_version,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)

    def project_generation_completed(
        self,
        event: ModelProjectionGenerationCompletedEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a node-generation-completed event into generation_events.

        Mirrors DelegationProjectionRunner._project_generation_completed; this is
        the path the live runtime actually invokes (OMN-12800). contract_yaml and
        handler_source are persisted in full — no truncation.
        """
        now = datetime.now(tz=UTC).isoformat()
        proof = compute_generation_proof_fields(
            contract_yaml=event.contract_yaml,
            handler_source=event.handler_source,
            routing_source=event.routing_source,
            resolved_endpoint=event.resolved_endpoint,
        )
        row: dict[str, object] = {
            "correlation_id": event.correlation_id,
            "task_description": event.task_description,
            "provider": event.provider,
            "model_id": event.model_id,
            "endpoint_class": event.endpoint_class,
            "attempt_count": event.attempt_count,
            "total_latency_e2e_ms": event.total_latency_e2e_ms,
            "contract_passed": event.contract_passed,
            "cost_inference_usd": event.cost_inference_usd,
            "timestamp": event.timestamp or now,
            "contract_yaml": event.contract_yaml,
            "handler_source": event.handler_source,
            **proof,
        }
        ok = db.upsert(GENERATION_TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(
            rows_upserted=1 if ok else 0, table=GENERATION_TABLE
        )

    def project_batch(
        self,
        events: list[ModelTaskDelegatedEvent],
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a batch of delegation events."""
        count = 0
        for event in events:
            result = self.project(event, db)
            count += result.rows_upserted
        return ModelProjectionResult(rows_upserted=count)


__all__: list[str] = [
    "GENERATION_PROJECTION_OWNER",
    "HandlerProjectionDelegation",
    "ModelProjectionGenerationCompletedEvent",
    "ModelProjectionResult",
    "ModelProjectionTaskDelegatedEvent",
    "ModelTaskDelegatedEvent",
    "compute_generation_proof_fields",
]


def _is_delegate_skill_terminal_payload(payload: dict[str, object]) -> bool:
    return (
        payload.get("correlation_id") is not None
        and payload.get("status") is not None
        and isinstance(payload.get("metrics"), dict)
    )


def _canonical_result_to_task_delegated_payload(
    payload: dict[str, object],
) -> dict[str, object]:
    quality_passed = bool(payload.get("quality_passed"))
    failure_reason = str(payload.get("failure_reason") or "")
    return {
        "correlation_id": payload.get("correlation_id"),
        "task_type": payload.get("task_type") or "unknown",
        "delegated_to": payload.get("model_used") or "unknown",
        "model_name": payload.get("model_used") or "",
        "quality_gate_passed": quality_passed,
        "quality_gates_failed": [failure_reason]
        if failure_reason and not quality_passed
        else [],
        "quality_gate_detail": failure_reason or None,
        "delegation_latency_ms": payload.get("latency_ms"),
        "prompt_text": payload.get("prompt_text"),
        "response_text": payload.get("content"),
        "tokens_input": payload.get("prompt_tokens") or 0,
        "tokens_output": payload.get("completion_tokens") or 0,
        "tokens_to_compliance": payload.get("tokens_to_compliance") or 0,
        "compliance_attempts": payload.get("compliance_attempts") or 1,
    }


def _gate_count(value: list[str] | None) -> int:
    return len(value or [])


def _preserve_existing_evidence(
    db: DatabaseAdapter,
    row: dict[str, object],
) -> None:
    """Keep terminal evidence when a sparse compatibility event arrives later."""
    correlation_id = row.get(CONFLICT_KEY)
    if not correlation_id:
        return
    existing_rows = db.query(TABLE, {CONFLICT_KEY: correlation_id})
    if not existing_rows:
        return
    existing = existing_rows[0]
    for key in ("prompt_text", "response_text"):
        if _is_blank(row.get(key)) and not _is_blank(existing.get(key)):
            row[key] = existing[key]
    for key in (
        "tokens_input",
        "tokens_output",
        "tokens_to_compliance",
        "cost_usd",
        "cost_savings_usd",
        "delegation_latency_ms",
        "pricing_manifest_version",
    ):
        if _is_zero(row.get(key)) and not _is_zero(existing.get(key)):
            row[key] = existing[key]
    if (
        _as_int(row.get("compliance_attempts")) <= 1
        and _as_int(existing.get("compliance_attempts")) > 1
    ):
        row["compliance_attempts"] = existing["compliance_attempts"]


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_zero(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float | Decimal):
        return value == 0
    if isinstance(value, str):
        try:
            return float(value) == 0.0
        except ValueError:
            return False
    return False


def _as_int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | Decimal):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0
