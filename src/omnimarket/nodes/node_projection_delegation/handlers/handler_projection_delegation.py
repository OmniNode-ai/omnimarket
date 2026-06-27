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

from omnibase_core.models.delegation.wire import ModelPremiumCounterfactual
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.delegation_judge_verdict import (
    ModelDelegationJudgeVerdictEvent,
)
from omnimarket.events.topics import (
    DELEGATE_SKILL_COMPLETED_TOPIC_V1,
    DELEGATE_SKILL_FAILED_TOPIC_V1,
)
from omnimarket.models.delegation.quality_bar_evidence import (
    extract_quality_bar_evidence,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
    ModelDelegationEventProjectionRow,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_budget_state import (
    ModelDelegationBudgetStateEvent,
    materialize_budget_state,
)
from omnimarket.pricing import recompute_actual_cost_and_savings
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "delegation_events"
CONFLICT_KEY = "correlation_id"
GENERATION_TABLE = "generation_events"
JUDGE_VERDICT_TABLE = "delegation_judge_verdict_events"
JUDGE_VERDICT_CONFLICT_KEY = "event_hash"

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
    # OMN-13166: behavioral verdict carried from the terminal benchmark, persisted
    # alongside contract_passed so the dashboard can show that a shape-valid
    # generation was behaviorally wrong (the gate-zero false-green). semantic_checked
    # records whether any behavioral fixture was applicable.
    semantic_checked: bool = Field(default=False)
    semantic_passed: bool = Field(default=False)
    # OMN-13289 (G0): validator-acceptance (corpus) verdict, carried from the
    # terminal benchmark and persisted alongside contract_passed/semantic_passed.
    # corpus_checked records whether the run carried a validator acceptance
    # corpus; corpus_passed is the deterministic corpus-execution verdict;
    # corpus_errors lists the per-fixture acceptance failures. The handler model
    # used extra="ignore" before OMN-13350, so these emitted fields were dropped
    # on the model side AND never written — and the columns did not exist, so the
    # INSERT raised UndefinedColumn and the whole completion event was dropped.
    corpus_checked: bool = Field(default=False)
    corpus_passed: bool = Field(default=False)
    corpus_errors: list[str] = Field(default_factory=list)
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
    context_pack_hash: str = Field(
        default="",
        description=(
            "Stable hash of the context pack injected into the delegated prompt. "
            "Empty string means the OFF arm or no context pack."
        ),
    )
    pricing_manifest_version: int = Field(
        default=0,
        ge=0,
        description="Version of the pricing manifest used to compute cost_savings_usd (OMN-10949).",
    )
    premium_counterfactual: ModelPremiumCounterfactual | None = Field(
        default=None,
        description=(
            "Pinned premium counterfactual {model, price, as_of, tokens, cost} "
            "carried from the durable task-delegated event (OMN-13355). Persisted "
            "as JSONB so the saving (counterfactual - actual) is auditable."
        ),
    )
    # OMN-13234: typed per-tier actual-cost measurement carried from the
    # task-delegated event, persisted to the cost_* columns added in migration
    # 0018 so the OTHER half of the saving (cost_usd actual) is auditable.
    cost_tier_type: str = Field(default="")
    cost_tier_name: str = Field(default="")
    cost_measurement_source: str = Field(default="")
    budget_headroom_consumed_usd: float = Field(default=0.0, ge=0.0)
    required_bar: float | None = Field(default=None, ge=0.0, le=1.0)
    actual_score: float | None = Field(default=None, ge=0.0, le=1.0)
    escalation_count: int = Field(default=0, ge=0)
    # OMN-13535: per-tier escalation attempt records carried from the terminal
    # event. Each entry that the orchestrator priced carries its own ``cost_usd``
    # (the metered spend that attempted tier incurred). The actual-cost recompute
    # adds these prior-attempt costs to the re-priced final-tier cost so a metered
    # tier that was attempted-but-rejected (escalated to a free tier) still
    # contributes its real cost to ``cost_usd`` — the recompute otherwise sees
    # only the final (free) tier and zeroes the row.
    escalation_history: tuple[dict[str, object], ...] = Field(default=())
    authority_source: str | None = Field(default=None)
    score_source: str | None = Field(default=None)
    request_override_applied: bool = Field(default=False)
    override_within_bounds: bool = Field(default=True)


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
        if "delegation-judge-verdict" in event_type:
            verdict = ModelDelegationJudgeVerdictEvent(**payload)
            result = self.project_judge_verdict(verdict, db_raw)
            return result.model_dump(mode="json")
        if (
            "node-generation-completed" in event_type
            or "node-generation-failed" in event_type
        ):
            # OMN-13468: both terminals (completed + failed) share the same payload
            # shape (ModelGenerationBenchmark) and write to generation_events. Only
            # contract_passed differs in value. Route failed terminal here so failed
            # runs are observable at GET /projection/node-generation-failed.v1.
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
        measurement = _measure_actual_cost(event)
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
            # OMN-13355: cost_usd is the MEASURED actual cost — the serving tier's
            # typed cost model (OMN-13234) priced against the measured tokens — not
            # the hardcoded 0.0 the workflow handler emits on the durable event.
            # cost_savings_usd is therefore counterfactual - real_actual, not
            # counterfactual - 0. See _measure_actual_cost for the fall-through to
            # the event values when no authoritative measurement is possible.
            "cost_usd": measurement.cost_usd,
            "cost_savings_usd": measurement.cost_savings_usd,
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
            "context_pack_hash": event.context_pack_hash,
            "pricing_manifest_version": event.pricing_manifest_version,
            # OMN-13355: persist the pinned premium counterfactual as JSONB so the
            # saving (counterfactual - actual) is auditable from the projection row.
            "premium_counterfactual": (
                event.premium_counterfactual.model_dump(mode="json")
                if event.premium_counterfactual is not None
                else None
            ),
            # OMN-13234/13355: persist the typed per-tier actual-cost measurement
            # provenance (columns from 0018). cost_measurement_source proves HOW
            # cost_usd was derived; the recompute validator asserts it is non-empty
            # whenever a non-zero saving is claimed.
            "cost_tier_type": measurement.cost_tier_type,
            "cost_tier_name": measurement.cost_tier_name,
            "cost_measurement_source": measurement.cost_measurement_source,
            "budget_headroom_consumed_usd": measurement.headroom_consumed_usd,
            "required_bar": event.required_bar,
            "actual_score": event.actual_score,
            "escalation_count": event.escalation_count,
            "authority_source": event.authority_source,
            "score_source": event.score_source,
            "request_override_applied": event.request_override_applied,
            "override_within_bounds": event.override_within_bounds,
        }
        evidence = extract_quality_bar_evidence(row)
        evidence.update(
            extract_quality_bar_evidence(
                {},
                checked_labels=event.quality_gates_checked or (),
            )
        )
        row.update(evidence)
        _preserve_existing_evidence(db, row)
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        # OMN-13235: event-source the per-tenant ceiling budget state. No-op for
        # free_local / metered tiers (no monthly cap); for budgeted tiers it draws
        # down the tenant's monthly headroom by the measured drawdown.
        materialize_budget_state(
            ModelDelegationBudgetStateEvent(
                correlation_id=event.correlation_id,
                cost_tier_name=measurement.cost_tier_name,
                cost_measurement_source=measurement.cost_measurement_source,
                budget_headroom_consumed_usd=_as_decimal(
                    measurement.headroom_consumed_usd
                ),
                cost_usd=_as_decimal(measurement.cost_usd),
                tenant_id=event.session_id,
                timestamp=event.timestamp,
            ),
            db,
        )
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
        timestamp_iso = row_model.timestamp.isoformat()
        row: dict[str, object] = {
            "correlation_id": str(row_model.correlation_id),
            "session_id": (
                str(row_model.session_id) if row_model.session_id is not None else None
            ),
            "timestamp": timestamp_iso,
            # OMN-13171: explicit created_at injection. The deployed
            # delegation_events schema declares created_at NOT NULL; a backing
            # store without an implicit DB default (the local SQLite evidence
            # target on a warm volume) raises a NOT NULL constraint when the
            # write omits it. Mirror the event timestamp — deterministic, not an
            # implicit datetime.now() at the DB layer (frozen-schema convention).
            "created_at": timestamp_iso,
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
            "context_pack_hash": row_model.context_pack_hash,
            "tokens_input": row_model.tokens_input,
            "tokens_output": row_model.tokens_output,
            "tokens_to_compliance": row_model.tokens_to_compliance,
            "compliance_attempts": row_model.compliance_attempts,
            "pricing_manifest_version": row_model.pricing_manifest_version,
            # OMN-13355: persist the pinned premium counterfactual as JSONB.
            "premium_counterfactual": (
                row_model.premium_counterfactual.model_dump(mode="json")
                if row_model.premium_counterfactual is not None
                else None
            ),
            "projection_version": row_model.projection_version,
            "reducer_version": row_model.reducer_version,
        }
        # OMN-13596: preserve an already-correct response_text when this
        # delegate-skill terminal event carries None/empty response_text.
        # Without this guard, a late-arriving timeout terminal (status="timeout",
        # response="") would clobber the real model answer written by an earlier
        # delegation-completed.v1 canonical event. _preserve_existing_evidence
        # retains the existing non-blank value when the incoming row has none.
        _preserve_existing_evidence(db, row)
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
            # OMN-13166: persist the behavioral verdict next to contract_passed.
            "semantic_checked": event.semantic_checked,
            "semantic_passed": event.semantic_passed,
            # OMN-13289 (G0) / OMN-13350: persist the validator-acceptance (corpus)
            # verdict. corpus_errors is a JSONB column — the sync DB adapter
            # JSON-adapts the list, so it is passed as a list here, not a JSON
            # string (the async runner path serializes its own $N::jsonb param).
            "corpus_checked": event.corpus_checked,
            "corpus_passed": event.corpus_passed,
            "corpus_errors": list(event.corpus_errors),
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

    def project_judge_verdict(
        self,
        event: ModelDelegationJudgeVerdictEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """UPSERT a reproducible judge verdict event into its evidence table."""
        row = _judge_verdict_projection_row(event)
        ok = db.upsert(JUDGE_VERDICT_TABLE, JUDGE_VERDICT_CONFLICT_KEY, row)
        return ModelProjectionResult(
            rows_upserted=1 if ok else 0, table=JUDGE_VERDICT_TABLE
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
    "JUDGE_VERDICT_TABLE",
    "HandlerProjectionDelegation",
    "ModelActualCostProjection",
    "ModelProjectionGenerationCompletedEvent",
    "ModelProjectionResult",
    "ModelProjectionTaskDelegatedEvent",
    "ModelTaskDelegatedEvent",
    "compute_generation_proof_fields",
    "validate_actual_cost_provenance",
]


def _judge_verdict_projection_row(
    event: ModelDelegationJudgeVerdictEvent,
) -> dict[str, object]:
    return {
        "event_hash": event.event_hash,
        "correlation_id": str(event.correlation_id),
        "task_type": event.task_type,
        "score_source": event.score_source,
        "judge_model": event.judge_model,
        "judge_model_version": event.judge_model_version,
        "judge_provider": event.judge_provider,
        "rubric_id": event.rubric_id,
        "rubric_hash": event.rubric_hash,
        "prompt_hash": event.prompt_hash,
        "input_hash": event.input_hash,
        "temperature": event.temperature,
        "judge_node_version": event.judge_node_version,
        "reasoning_hash": event.reasoning_hash,
        "verdict": event.verdict.value,
        "actual_score": event.actual_score,
        "failure_kind": event.failure_kind,
        "failure_message": event.failure_message,
    }


def _is_delegate_skill_terminal_payload(payload: dict[str, object]) -> bool:
    return (
        payload.get("correlation_id") is not None
        and payload.get("status") is not None
        and isinstance(payload.get("metrics"), dict)
    )


def _winning_metered_tier_name(escalation_history: object) -> str:
    """Return the LAST escalation tier that recorded a positive metered ``cost_usd``.

    OMN-13408 (canonical projection). The canonical ``delegation-failed.v1``
    terminal (``ModelDelegationResult``) carries the real metered spend in
    ``cumulative_attempt_cost`` / ``final_attempt_cost`` and in the per-tier
    ``escalation_history`` records, but it has NO top-level ``cost_tier_name``.
    Without a serving-tier name the projection's ``_measure_actual_cost`` takes the
    "unknown serving tier" fall-through and persists the event's ``cost_usd``
    (defaulted to 0.0) — flooring a row whose escalation_history holds real metered
    spend.

    The winning tier is the last attempt that actually incurred metered cost (the
    free local tier records ``cost_usd=0.0`` and is skipped). Empty string when no
    metered attempt is found — a free-only failed terminal honestly stays 0.
    Accepts the raw ``payload.get("escalation_history")`` value (``object``) and
    fails closed (empty string) on any non-iterable / malformed shape.
    """
    if not isinstance(escalation_history, list | tuple):
        return ""
    winner = ""
    for attempt in escalation_history:
        if not isinstance(attempt, dict):
            continue
        raw_cost = attempt.get("cost_usd")
        tier_name = attempt.get("tier_name")
        if (
            isinstance(raw_cost, int | float)
            and float(raw_cost) > 0.0
            and isinstance(tier_name, str)
            and tier_name
        ):
            winner = tier_name
    return winner


# OMN-13596: canonical delegation-timeout sentinel substrings. The orchestrator
# contract emits ``"Delegation timed out before runtime completion"`` and the
# runtime dispatch port (omnibase_infra ports/port_runtime_delegation_dispatch.py)
# returns ``"timed out after <N>s waiting for delegation result"`` on the
# caller-side Kafka-wait timeout. Either string can land in the canonical
# terminal ``content`` field; it is NEVER a model answer and must not be
# projected into ``response_text`` on a row the projection treats as a metered
# PASS. Matched case-insensitively as substrings so a wrapped/prefixed variant
# (e.g. "Delegation timed out before runtime completion (...)") is still caught.
_DELEGATION_TIMEOUT_SENTINELS: tuple[str, ...] = (
    "timed out before runtime completion",
    "timed out after",
)


def _is_delegation_timeout_string(value: object) -> bool:
    """Return True when ``value`` is a canonical delegation timeout/error string.

    OMN-13596. On the canonical terminal path the ``content`` field can carry the
    caller-side delegation-wait timeout text rather than the model's output. A
    PASS row must never project that string into ``response_text`` (a customer
    reading a successful, metered delegation would otherwise see a timeout
    string). Fails closed (``False``) on any non-string shape so a genuine answer
    is never suppressed.
    """
    if not isinstance(value, str):
        return False
    lowered = value.casefold()
    return any(sentinel in lowered for sentinel in _DELEGATION_TIMEOUT_SENTINELS)


def _canonical_response_text(
    payload: dict[str, object],
    *,
    quality_passed: bool,
) -> str | None:
    """Resolve the canonical terminal's ``response_text`` projection value.

    OMN-13596. The canonical ``delegation-completed.v1`` / ``delegation-failed.v1``
    terminal carries the model output in ``content``. On the authoritative metered
    PASS row that ``content`` must be the model's actual answer. When the terminal
    instead carries a delegation timeout/error string in ``content`` (the
    caller-side Kafka-wait timeout text), projecting it onto a PASS row makes a
    success look like a failure. Suppress it to ``None`` on a PASS so the UPSERT's
    COALESCE + ``_preserve_existing_evidence`` keep an already-written genuine
    answer instead of clobbering it with the timeout string. The FAILED path is
    unchanged: a failure honestly surfaces its terminal ``content``.
    """
    content = payload.get("content")
    if quality_passed and _is_delegation_timeout_string(content):
        return None
    return content if isinstance(content, str) else None


def _canonical_result_to_task_delegated_payload(
    payload: dict[str, object],
) -> dict[str, object]:
    quality_passed = bool(payload.get("quality_passed"))
    failure_reason = str(payload.get("failure_reason") or "")
    escalation_history = payload.get("escalation_history") or ()

    # OMN-13408 (canonical FAILED-terminal cost resolution): the canonical
    # ``delegation-failed.v1`` event (``ModelDelegationResult``) co-writes the same
    # ``delegation_events`` row as the compat ``task-delegated.v1`` event, but it
    # has NO top-level ``cost_usd`` / ``cost_tier_name`` — the real metered spend
    # lives in ``cumulative_attempt_cost`` / ``final_attempt_cost`` and in the
    # winning ``escalation_history`` tier. Dropping those (the prior converter
    # carried none of them) made the projection floor the canonical write to
    # ``cost_usd=0.0`` with an empty serving tier, clobbering the compat event's
    # honest cost (live STRIKE THREE, CID 5120dd9c: emitter said 0.01924, row 0.0).
    #
    # Carry the canonical cumulative spend as ``cost_usd`` and resolve the serving
    # tier from the winning metered ``escalation_history`` entry so the projection's
    # ``_measure_actual_cost`` trusts the metered total instead of flooring to 0.
    # ``cumulative_attempt_cost`` is the total across all attempted tiers (counted
    # once); fall back to ``final_attempt_cost`` for older emitters.
    cumulative_cost = payload.get("cumulative_attempt_cost")
    if not isinstance(cumulative_cost, int | float) or float(cumulative_cost) <= 0.0:
        cumulative_cost = payload.get("final_attempt_cost")
    cost_usd = (
        float(cumulative_cost)
        if isinstance(cumulative_cost, int | float) and float(cumulative_cost) > 0.0
        else 0.0
    )
    winning_tier = _winning_metered_tier_name(escalation_history)
    # OMN-13649: prefer the AUTHORITATIVE serving tier carried on the canonical
    # terminal (``cost_tier_name`` = ``workflow.current_tier_name`` from the
    # routing decision). This is the tier-drop fix: a COMPLETED local/free
    # delegation has no metered escalation_history winner, so the prior
    # ``_winning_metered_tier_name`` fallback resolved to '' and the projection
    # wrote an empty tier for the most common path. Fall back to the metered
    # winner only when the terminal predates this field (back-compat).
    raw_terminal_tier = payload.get("cost_tier_name")
    terminal_tier = raw_terminal_tier if isinstance(raw_terminal_tier, str) else ""
    resolved_tier = terminal_tier or winning_tier

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
        # OMN-13644: carry the canonical terminal's context-pack hash through the
        # converter so the row mapping reads the real value (non-empty when the
        # request carried a context pack) instead of the '' field default. The
        # orchestrator now persists this on COMPLETED and FAILED/ESCALATED terminals.
        "context_pack_hash": payload.get("context_pack_hash") or "",
        "prompt_text": payload.get("prompt_text"),
        # OMN-13596: never project a delegation timeout/error string into
        # response_text on a PASS row. _canonical_response_text suppresses the
        # caller-side timeout string (returns None) when quality_passed is True,
        # so the UPSERT COALESCE / _preserve_existing_evidence keep the real
        # model answer instead of overwriting it with "Timed out…".
        "response_text": _canonical_response_text(
            payload, quality_passed=quality_passed
        ),
        "tokens_input": payload.get("prompt_tokens") or 0,
        "tokens_output": payload.get("completion_tokens") or 0,
        "tokens_to_compliance": payload.get("tokens_to_compliance") or 0,
        "compliance_attempts": payload.get("compliance_attempts") or 1,
        "required_bar": payload.get("required_bar"),
        "actual_score": payload.get("actual_score") or payload.get("quality_score"),
        "escalation_count": payload.get("escalation_count") or 0,
        # OMN-13408: the metered total + serving tier carried from the canonical
        # terminal. With cost_tier_name set, _measure_actual_cost re-prices/trusts
        # the metered cost instead of taking the unknown-tier 0.0 fall-through.
        # OMN-13649: cost_tier_name is now the AUTHORITATIVE serving tier from the
        # terminal (falling back to the metered escalation winner for pre-OMN-13649
        # terminals), so a COMPLETED free/local row carries its real tier instead
        # of '' — and _measure_actual_cost derives cost_tier_type from it.
        "cost_usd": cost_usd,
        "cost_tier_name": resolved_tier,
        # OMN-13535: carry the per-tier attempt records (each with its priced
        # cost_usd) so the actual-cost recompute can add the prior metered tiers'
        # spend to the re-priced final tier on the completed path.
        "escalation_history": escalation_history,
        "authority_source": payload.get("authority_source")
        or payload.get("required_bar_source"),
        "score_source": payload.get("score_source"),
        "request_override_applied": payload.get("request_override_applied") or False,
        "override_within_bounds": payload.get("override_within_bounds") is not False,
    }


class ModelActualCostProjection(BaseModel):
    """Resolved actual-cost figures written onto a delegation_events row.

    OMN-13355: the projection's authoritative cost provenance. cost_usd is a
    MEASUREMENT (the serving tier's typed cost model priced against the measured
    tokens), not the workflow handler's hardcoded 0.0. cost_savings_usd is
    counterfactual - real_actual. The recompute validator asserts that any row
    claiming a non-zero saving carries a non-empty cost_measurement_source.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cost_usd: float = Field(default=0.0)
    cost_savings_usd: float = Field(default=0.0)
    headroom_consumed_usd: float = Field(default=0.0, ge=0.0)
    cost_tier_type: str = Field(default="")
    cost_tier_name: str = Field(default="")
    cost_measurement_source: str = Field(default="")


def _sum_escalation_attempt_costs(
    escalation_history: tuple[dict[str, object], ...],
) -> float:
    """Sum the per-tier ``cost_usd`` recorded across escalation attempts (OMN-13535).

    Each attempt record the orchestrator priced carries its own ``cost_usd`` (the
    metered spend that attempted tier incurred). Attempts emitted before OMN-13535
    (or non-priced records) default the field to absent/0.0, so the sum is 0.0 and
    the recompute degrades to its prior single-tier behavior — no regression for
    non-escalated rows.
    """
    total = 0.0
    for attempt in escalation_history:
        raw_cost = attempt.get("cost_usd") if isinstance(attempt, dict) else None
        if isinstance(raw_cost, int | float):
            total += float(raw_cost)
    return total


def _measure_actual_cost(
    event: ModelProjectionTaskDelegatedEvent,
) -> ModelActualCostProjection:
    """Resolve the MEASURED actual cost + honest savings for one delegation row.

    OMN-13355 wiring. The workflow handler emits the durable task-delegated event
    with ``cost_usd`` hardcoded to 0.0, so a saving of ``counterfactual - cost_usd``
    is really ``counterfactual - 0`` — the full counterfactual, overstated by the
    serving tier's real (non-zero, for metered / budgeted-overage) cost. When the
    event carries a pinned premium counterfactual and a serving tier name, this
    re-prices the measured tokens through that tier's typed cost model and returns
    ``cost_savings_usd = counterfactual - measured_actual``.

    Fall-through (preserves the event's own values, no recompute):
      * No cost_tier_name — the serving tier is unknown, so the typed cost model
        cannot be resolved; keep the event values + carried provenance.

    OMN-13408 (FAILED / escalation path, ``premium_counterfactual=None``): the
    terminal's own ``cost_usd`` is the AUTHORITATIVE total — ``_emit_terminal``
    already sums the final tier's measured cost plus the prior attempted tiers'
    banked metered spend, counting the terminal attempt exactly once (its
    escalation_history entry is NOT re-banked). So when the terminal carried a
    non-zero ``cost_usd`` the projection trusts it verbatim (no re-add of
    escalation_history, which would double-count the terminal tier). But when the
    terminal carried ``cost_usd=0.0`` despite a metered serving tier with real
    served tokens — the live defect (CID 21077717: FAILED, escalation_count=1,
    metered ``cheap_cloud`` glm-5.2, served input=103/output=1777, but
    ``cost_usd=0.0`` persisted while a savings number was quoted) — the projection
    re-measures the served tokens through the SAME ``recompute_actual_cost_and_savings``
    the completed/savings path uses, so ``cost_usd > 0`` whenever metered tokens
    were served. The terminal cost is the source of truth; this re-measurement is
    its honest floor, not a second estimate path. The saving stays 0 with no
    counterfactual baseline, but the measured cost is honest — closing the
    dual-path (tokens-carry, cost-does-not) inconsistency the ticket forbids.
    """
    if not event.cost_tier_name:
        # Unknown serving tier (e.g. legacy zero-token golden-chain rows, or a
        # remote-agent A2A terminal with no tier): the typed cost model cannot be
        # resolved, so keep the event values + carried provenance verbatim.
        return ModelActualCostProjection(
            cost_usd=event.cost_usd,
            cost_savings_usd=event.cost_savings_usd,
            headroom_consumed_usd=event.budget_headroom_consumed_usd,
            cost_tier_type=event.cost_tier_type,
            cost_tier_name=event.cost_tier_name,
            cost_measurement_source=event.cost_measurement_source,
        )

    measurement = recompute_actual_cost_and_savings(
        tier_name=event.cost_tier_name,
        prompt_tokens=event.tokens_input,
        completion_tokens=event.tokens_output,
        premium_counterfactual=event.premium_counterfactual,
    )

    if event.premium_counterfactual is None:
        # FAILED / escalation terminal (OMN-13408). The terminal's ``cost_usd``
        # is the authoritative total (final + prior, counted once). Trust it when
        # it is already non-zero — re-adding escalation_history here would
        # double-count the terminal tier (its own entry is in that history). Only
        # when the terminal lost the cost (0.0) despite a metered serving tier
        # with served tokens do we substitute the tier-priced floor so a row with
        # real metered tokens can never persist ``cost_usd=0``.
        floor_cost_usd = measurement.cash_cost_usd
        total_cost_usd = event.cost_usd if event.cost_usd > 0.0 else floor_cost_usd
        # No auditable counterfactual baseline on the failure path, so there is no
        # honest saving to report — never quote ``counterfactual - 0``.
        return ModelActualCostProjection(
            cost_usd=total_cost_usd,
            cost_savings_usd=0.0,
            headroom_consumed_usd=measurement.headroom_consumed_usd,
            cost_tier_type=measurement.cost_tier_type,
            cost_tier_name=measurement.cost_tier_name,
            cost_measurement_source=measurement.cost_measurement_source,
        )

    # COMPLETED / accepted path (OMN-13535): the recompute prices only the FINAL
    # accepted tier. Earlier metered tiers that ran and were rejected before the
    # final tier was accepted carry their per-tier spend in ``escalation_history``;
    # add it so an accepted-on-free escalation that burned metered budget reports
    # the real total cost (and honest saving = counterfactual - total), instead of
    # zeroing to the free final tier.
    prior_attempt_cost_usd = _sum_escalation_attempt_costs(event.escalation_history)
    total_cost_usd = measurement.cash_cost_usd + prior_attempt_cost_usd
    total_savings_usd = measurement.cost_savings_usd - prior_attempt_cost_usd
    return ModelActualCostProjection(
        cost_usd=total_cost_usd,
        cost_savings_usd=total_savings_usd,
        headroom_consumed_usd=measurement.headroom_consumed_usd,
        cost_tier_type=measurement.cost_tier_type,
        cost_tier_name=measurement.cost_tier_name,
        cost_measurement_source=measurement.cost_measurement_source,
    )


def validate_actual_cost_provenance(row: dict[str, object]) -> None:
    """Assert a delegation_events row's savings is backed by a measured actual cost.

    OMN-13355 recompute validator. A row that claims a non-zero ``cost_savings_usd``
    must prove its provenance:

      1. ``cost_measurement_source`` is non-empty — it records HOW ``cost_usd`` was
         measured (free_local | metered | budgeted_* | no_cost_model). An empty
         source means the saving was computed against the hardcoded 0.0, the exact
         bug this ticket closes.
      2. If a ``premium_counterfactual`` is present, the saving reconciles:
         ``cost_savings_usd == counterfactual_cost_usd - cost_usd`` (within a
         Decimal tolerance). This is the audit invariant — a verifier recomputes
         the saving from the persisted counterfactual and actual cost.

    Raises ``ValueError`` on a provenance gap so the projection's own tests and
    the OCC evidence path can assert the invariant. Side-effect free.
    """
    savings = _as_decimal(row.get("cost_savings_usd"))
    cost = _as_decimal(row.get("cost_usd"))
    source = str(row.get("cost_measurement_source") or "")
    if savings != Decimal("0") and not source:
        raise ValueError(
            "delegation_events row claims a non-zero cost_savings_usd but carries "
            "no cost_measurement_source — savings was computed without an "
            "actual-cost measurement (OMN-13355 provenance gap)"
        )
    counterfactual = row.get("premium_counterfactual")
    if isinstance(counterfactual, dict):
        cf_cost = _as_decimal(counterfactual.get("counterfactual_cost_usd"))
        if abs((cf_cost - cost) - savings) > Decimal("0.000001"):
            raise ValueError(
                "delegation_events row savings does not reconcile: "
                f"counterfactual_cost_usd({cf_cost}) - cost_usd({cost}) != "
                f"cost_savings_usd({savings}) (OMN-13355 audit invariant)"
            )


def _as_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except (ValueError, ArithmeticError):
            return Decimal("0")
    return Decimal("0")


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
    for key in ("prompt_text", "response_text", "context_pack_hash"):
        if _is_blank(row.get(key)) and not _is_blank(existing.get(key)):
            row[key] = existing[key]
    # OMN-13596: a confirmed PASS row's response_text must never be overwritten
    # by a later FAILED/timeout terminal's error string. When the existing row
    # has quality_gate_passed=True and the incoming row has quality_gate_passed=False,
    # preserve the existing response_text (which carries the real model answer)
    # regardless of whether the incoming row's response_text is blank.
    if bool(existing.get("quality_gate_passed")) and not bool(
        row.get("quality_gate_passed")
    ):
        existing_response = existing.get("response_text")
        if not _is_blank(existing_response):
            row["response_text"] = existing_response
    for key in (
        "tokens_input",
        "tokens_output",
        "tokens_to_compliance",
        "cost_usd",
        "cost_savings_usd",
        "delegation_latency_ms",
        "pricing_manifest_version",
        "required_bar",
        "actual_score",
        "escalation_count",
    ):
        if _is_zero(row.get(key)) and not _is_zero(existing.get(key)):
            row[key] = existing[key]
    for key in ("authority_source", "score_source"):
        if _is_blank(row.get(key)) and not _is_blank(existing.get(key)):
            row[key] = existing[key]
    if bool(existing.get("request_override_applied")):
        row["request_override_applied"] = True
    if existing.get("override_within_bounds") is False:
        row["override_within_bounds"] = False
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
