# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation skill handler — domain translator with an injected dispatch port.

This handler translates consumer-facing delegation requests into runtime-internal
delegation commands. It owns no transport detail: the dispatch port it receives at
construction is runtime-owned and resolved through dependency injection. The
handler never names a wire address, a broker, a message bus subject, or any adapter
internal.
"""

from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID

from omnibase_core.models.delegation.wire import (
    EnumDelegationTerminalFailureCause,
    EnumQualityScoreComparison,
    ModelPremiumCounterfactual,
)

from omnimarket.config import get_settings
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    ModelDelegateSkillResponse,
    ModelDelegateSkillResponseMetrics,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
    ProtocolDelegationEventBus,
)
from omnimarket.pricing import (
    DEFAULT_BASELINE_MODEL,
    build_premium_counterfactual,
    estimate_baseline_cost_usd,
    estimate_frontier_costs_usd,
    get_manifest_version_int,
)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "timeout"})


class ProtocolDelegationDispatchPort(Protocol):
    """Injected port for delegation dispatch. Implementation is runtime-owned.

    OMN-13161: ``max_tokens`` is ``int | None``. ``None`` means the request
    omitted an explicit budget; the dispatch implementation resolves the effective
    value from the selected backend's per-backend ceiling in the routing contract.

    OMN-15180: ``backend_id`` is ``str | None``, default ``None``. ``None``
    preserves the pre-existing cheapest-first tier_order resolution.
    ``LocalDelegationDispatchPort`` (bus-less local path) honors a non-None pin
    end-to-end via the OMN-15156 seam. ``RuntimeDelegationDispatchPort`` (deployed
    bus path) declares the same parameter to satisfy this Protocol but does not
    yet thread it downstream — see that port's docstring for the explicit
    fail-loud boundary.

    OMN-15193: ``response_contract`` is ``dict[str, object] | None``, default
    ``None``. ``None`` preserves the exact pre-existing quality-gate behavior
    (task-class keyword heuristics). A non-None value is a caller-declared JSON
    Schema; ``LocalDelegationDispatchPort`` threads it into the quality-gate
    reducer, where structural schema validation REPLACES the keyword heuristics
    for that request. ``RuntimeDelegationDispatchPort`` declares the same
    parameter to satisfy this Protocol but does not yet thread it downstream —
    see that port's docstring for the explicit fail-loud boundary.

    OMN-15482: ``system_prompt`` (``str | None``), ``temperature``
    (``float | None``) and ``response_format`` (``dict[str, object] | None``)
    are the three completion-shaping parameters that close the measured
    fidelity gap against a direct OpenAI-compatible chat-completions call.
    ``None`` on each preserves the exact pre-existing behavior: the task-type
    default system prompt, the effect-layer default temperature, and no
    ``response_format`` key on the outbound payload respectively.
    ``LocalDelegationDispatchPort`` threads all three through to the outbound
    chat-completions payload. ``RuntimeDelegationDispatchPort`` declares them to
    satisfy this Protocol but fails loud on a non-None value — the same boundary
    as ``backend_id``/``response_contract`` above.
    """

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: UUID,
        max_tokens: int | None,
        source_file_path: str | None,
        source_session_id: str | None,
        wait: bool,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
        tenant_id: str | None,
        backend_id: str | None = None,
        response_contract: dict[str, object] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        response_format: dict[str, object] | None = None,
    ) -> dict[str, object]: ...


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_optional_float(value: object) -> float | None:
    """Parse an optional numeric field without inventing malformed truth."""
    if value is None:
        return None
    if isinstance(value, bool):
        msg = f"expected optional float, got bool {value!r}"
        raise ValueError(msg)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError as exc:
            msg = f"expected optional float, got {value!r}"
            raise ValueError(msg) from exc
    msg = f"expected optional float, got {value!r}"
    raise ValueError(msg)


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(item) for item in value]
    if isinstance(value, str) and value:
        return [value]
    return []


def _as_quality_score_comparison(
    value: object,
) -> EnumQualityScoreComparison | None:
    if value is None:
        return None
    if isinstance(value, EnumQualityScoreComparison):
        return value
    if isinstance(value, str):
        return EnumQualityScoreComparison(value)
    msg = f"invalid score_vs_required_bar {value!r}"
    raise ValueError(msg)


def _as_terminal_failure_cause(
    value: object,
) -> EnumDelegationTerminalFailureCause | None:
    if value is None:
        return None
    if isinstance(value, EnumDelegationTerminalFailureCause):
        return value
    if isinstance(value, str):
        return EnumDelegationTerminalFailureCause(value)
    msg = f"invalid terminal_failure_cause {value!r}"
    raise ValueError(msg)


def _measured_cost_usd(result: dict[str, object]) -> float:
    """Resolve total metered spend from canonical and compatibility shapes.

    Match the Infra runtime normalizer's canonical invariant exactly: when either
    attempt-cost field is present and non-negative, actual spend is the maximum of
    ``cumulative_attempt_cost`` and ``final_attempt_cost``.  This prevents a
    malformed/defaulted cumulative value from understating the final attempt and
    prevents a stale compatibility ``cost_usd`` from overriding canonical truth.
    Local/legacy ports that carry neither canonical field still use ``cost_usd``.
    """
    canonical_costs: list[float] = []
    for key in ("cumulative_attempt_cost", "final_attempt_cost"):
        value = result.get(key)
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and value >= 0.0
        ):
            canonical_costs.append(float(value))
    if canonical_costs:
        return max(canonical_costs)
    return max(_as_float(result.get("cost_usd")), 0.0)


def _counterfactual_token_counts(result: dict[str, object]) -> tuple[int, int]:
    """Resolve the canonical cumulative token basis with compatibility fallbacks."""

    def _resolve(
        cumulative_key: str,
        normalized_key: str,
        legacy_key: str,
    ) -> int:
        cumulative = result.get(cumulative_key)
        if not isinstance(cumulative, bool):
            parsed_cumulative = _as_int(cumulative, default=-1)
            if parsed_cumulative >= 0:
                return parsed_cumulative
        return _as_int(result.get(normalized_key, result.get(legacy_key, 0)))

    return (
        _resolve("cumulative_input_tokens", "input_tokens", "prompt_tokens"),
        _resolve("cumulative_output_tokens", "output_tokens", "completion_tokens"),
    )


def _estimate_claude_cost_savings(
    result: dict[str, object],
    *,
    actual_cost_usd: float,
) -> float:
    prompt_tokens, completion_tokens = _counterfactual_token_counts(result)
    counterfactual_cost_usd = estimate_baseline_cost_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    return round(max(counterfactual_cost_usd - actual_cost_usd, 0.0), 6)


def _frontier_cost_estimates(result: dict[str, object]) -> dict[str, float]:
    prompt_tokens, completion_tokens = _counterfactual_token_counts(result)
    return estimate_frontier_costs_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def _attempt_records(
    result: dict[str, object],
) -> list[ModelDelegateSkillAttemptRecord]:
    """Build the typed per-tier attempt ladder (OMN-14063).

    ``result["attempts"]`` is the richer dispatch-port-owned record and always
    wins when present. Canonical bus terminals currently expose only serialized
    ``escalation_history``; map that as documented best-effort evidence rather
    than dropping it. History contains rejected attempts and may lack backend
    aliases or the accepted terminal attempt, so callers must use the separate
    ``attempts_count`` as the total-call authority.
    """
    raw_attempts = result.get("attempts")
    from_escalation_history = not isinstance(raw_attempts, list)
    if isinstance(raw_attempts, list):
        attempt_values = raw_attempts
    else:
        raw_history = result.get("escalation_history")
        if not isinstance(raw_history, list | tuple):
            return []
        attempt_values = list(raw_history)

    records: list[ModelDelegateSkillAttemptRecord] = []
    for raw in attempt_values:
        if not isinstance(raw, dict):
            continue
        if from_escalation_history:
            failure_reasons = _as_str_list(raw.get("failure_reasons"))
            records.append(
                ModelDelegateSkillAttemptRecord(
                    tier=str(raw.get("tier_name") or raw.get("tier") or ""),
                    backend_id=str(
                        raw.get("backend_id") or raw.get("routing_decision_id") or ""
                    ),
                    model_id=str(raw.get("model_used") or raw.get("model_id") or ""),
                    # Escalation history records rejected/failed attempts. It does
                    # not contain the accepted terminal attempt.
                    quality_gate_passed=False,
                    quality_score=(
                        _as_float(raw["quality_score"])
                        if raw.get("quality_score") is not None
                        else None
                    ),
                    cost_usd=_as_float(raw.get("cost_usd")),
                    failure_class=(
                        str(raw["failure_class"])
                        if raw.get("failure_class") is not None
                        else None
                    ),
                    error_message="; ".join(failure_reasons),
                )
            )
            continue
        records.append(
            ModelDelegateSkillAttemptRecord(
                tier=str(raw.get("tier", "")),
                backend_id=str(raw.get("backend_id", "")),
                model_id=str(raw.get("model_id", "")),
                quality_gate_passed=bool(raw.get("quality_gate_passed", False)),
                quality_score=(
                    _as_float(raw["quality_score"])
                    if raw.get("quality_score") is not None
                    else None
                ),
                cost_usd=_as_float(raw.get("cost_usd")),
                failure_class=(
                    str(raw["failure_class"])
                    if raw.get("failure_class") is not None
                    else None
                ),
                error_message=str(raw.get("error_message", "")),
            )
        )
    return records


def _response_attempts_count(
    result: dict[str, object],
    attempts: list[ModelDelegateSkillAttemptRecord],
) -> int:
    """Keep an explicit terminal count authoritative; derive only for legacy ports."""
    if result.get("attempts_count") is not None:
        return _as_int(result["attempts_count"], default=1)
    return max(
        1,
        len(attempts),
        _as_int(result.get("compliance_attempts"), default=1),
        _as_int(result.get("escalation_count")) + 1,
    )


def _premium_counterfactual(
    result: dict[str, object],
) -> ModelPremiumCounterfactual | None:
    """Build the pinned premium counterfactual from measured tokens (OMN-13355)."""
    prompt_tokens, completion_tokens = _counterfactual_token_counts(result)
    premium_model = str(
        result.get("model_cloud_baseline")
        or result.get("baseline_model")
        or DEFAULT_BASELINE_MODEL
    )
    return build_premium_counterfactual(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        premium_model=premium_model,
    )


def _response_from_result(
    request: ModelDelegateSkillRequest,
    result: dict[str, object],
    *,
    tenant_id: str | None,
) -> ModelDelegateSkillResponse:
    raw_status = str(result.get("status", "completed"))
    is_known_status = raw_status in _TERMINAL_STATUSES
    status_value: Literal["completed", "failed", "timeout"] = (
        raw_status  # type: ignore[assignment]
        if is_known_status
        else "failed"
    )
    error_message = str(
        result.get("error_message") or result.get("failure_reason") or ""
    )
    if not is_known_status:
        error_message = f"runtime returned unknown terminal status {raw_status!r}"
    quality_failures = _as_str_list(
        result.get("quality_gates_failed", result.get("failure_reason", ""))
    )
    quality_gate_passed = bool(
        result.get("quality_gate_passed", result.get("quality_passed", False))
    )
    actual_cost_usd = _measured_cost_usd(result)
    cost_savings_usd = (
        max(
            _as_float(
                result.get("cost_savings_usd"),
                default=_estimate_claude_cost_savings(
                    result,
                    actual_cost_usd=actual_cost_usd,
                ),
            ),
            0.0,
        )
        if status_value == "completed" and quality_gate_passed
        else 0.0
    )
    attempts = _attempt_records(result)
    return ModelDelegateSkillResponse(
        status=status_value,
        correlation_id=request.correlation_id,
        task_type=request.task_type,
        # OMN-14485: carry the resolved tenant onto the response so the terminal
        # event this becomes stamps a real tenant on the projection row.
        tenant_id=tenant_id,
        provider=str(result.get("delegated_to") or result.get("endpoint_url") or ""),
        model_name=str(result.get("model_name") or result.get("model_used") or ""),
        model_cloud_baseline=str(
            result.get("model_cloud_baseline")
            or result.get("baseline_model")
            or DEFAULT_BASELINE_MODEL
        ),
        pricing_manifest_version=_as_int(
            result.get("pricing_manifest_version"),
            default=get_manifest_version_int(),
        ),
        prompt_text=request.prompt,
        response=str(result.get("content", "")),
        quality_gate_passed=quality_gate_passed,
        quality_score=_as_float(result.get("quality_score")),
        required_quality_bar=_as_optional_float(result.get("required_quality_bar")),
        score_vs_required_bar=_as_quality_score_comparison(
            result.get("score_vs_required_bar")
        ),
        failed_acceptance_criteria=tuple(
            _as_str_list(result.get("failed_acceptance_criteria"))
        ),
        terminal_failure_cause=_as_terminal_failure_cause(
            result.get("terminal_failure_cause")
        ),
        quality_gates_failed=quality_failures,
        error_message=error_message,
        metrics=ModelDelegateSkillResponseMetrics(
            input_tokens=_as_int(
                result.get("input_tokens", result.get("prompt_tokens", 0))
            ),
            output_tokens=_as_int(
                result.get("output_tokens", result.get("completion_tokens", 0))
            ),
            total_tokens=_as_int(result.get("total_tokens")),
            tokens_to_compliance=_as_int(result.get("tokens_to_compliance")),
            compliance_attempts=_as_int(result.get("compliance_attempts")),
            cost_usd=actual_cost_usd,
            cost_savings_usd=cost_savings_usd,
            frontier_costs_usd=_frontier_cost_estimates(result),
            premium_counterfactual=_premium_counterfactual(result),
            latency_ms=_as_int(
                result.get("delegation_latency_ms", result.get("latency_ms", 0))
            ),
        ),
        escalation_count=_as_int(result.get("escalation_count")),
        attempts_count=_response_attempts_count(result, attempts),
        attempts=attempts,
    )


class HandlerDelegateSkill:
    """Translate a typed delegation request to a runtime command via the port."""

    def __init__(
        self,
        event_bus: ProtocolDelegationEventBus | None = None,
        *,
        dispatch_port: ProtocolDelegationDispatchPort | None = None,
    ) -> None:
        if dispatch_port is not None:
            self._dispatch_port: ProtocolDelegationDispatchPort = dispatch_port
        else:
            # Transport-aware port selection is owned by the ports package, not
            # this domain handler. A bus-less or in-memory single-process runtime
            # resolves to the in-process local port (routing + canonical effect +
            # quality gate + sqlite evidence row, OMN-13160/OMN-13601); an external
            # broker bus resolves to the runtime publish/await port. Imported
            # lazily to avoid a construction-time import cycle with the ports
            # package, which references this handler's port protocol.
            from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_selection import (
                select_delegation_dispatch_port,
            )

            self._dispatch_port = select_delegation_dispatch_port(event_bus)

    async def handle(
        self, request: ModelDelegateSkillRequest
    ) -> ModelDelegateSkillResponse:
        """Dispatch the request and return a typed response.

        On any dispatch exception, returns ``status="failed"`` with the error text
        rather than propagating — failed delegations must remain observable.
        """
        # OMN-14485: resolve the tenant identity ONCE at request-acceptance and
        # carry it onto the response (and thus the auto-published terminal event
        # node_projection_delegation reads). Precedence mirrors the local dispatch
        # port and HandlerDelegationWorkflow: a verified request-carried tenant_id
        # wins; otherwise the ONEX_TENANT_ID interim (OMN-14058) applies; else None.
        # The dispatch port still receives the verified request tenant_id (OMN-14349
        # seam) — the env-var interim is a projection-stamping fallback, not a
        # verified-identity source at the port boundary.
        resolved_tenant_id = request.tenant_id or get_settings().onex_tenant_id or None
        try:
            result = await self._dispatch_port.dispatch(
                prompt=request.prompt,
                task_type=request.task_type,
                correlation_id=request.correlation_id,
                max_tokens=request.max_tokens,
                source_file_path=request.source_file_path,
                source_session_id=request.session_id
                or request.metadata.get("session_id"),
                wait=request.wait,
                quality_contract_mode=request.quality_contract_mode,
                acceptance_criteria=request.acceptance_criteria,
                # OMN-14349: thread the verified tenant_id (stamped upstream by
                # OMN-14208 Path A's ingress node from a verified source, never
                # self-reported) to the dispatch port. A stamp that stops here is
                # dead on arrival -- this is the seam pinned by
                # test_handler_propagates_verified_tenant_id_to_dispatch_port.
                tenant_id=request.tenant_id,
                # OMN-15180: thread the optional wire-level backend pin to the
                # dispatch port. A pin that stops here is dead on arrival -- this
                # is the seam pinned by
                # test_handler_propagates_backend_id_pin_to_dispatch_port.
                backend_id=request.backend_id,
                # OMN-15193: thread the optional wire-level declared response
                # contract to the dispatch port. A contract that stops here is
                # dead on arrival -- this is the seam pinned by
                # test_handler_propagates_response_contract_to_dispatch_port.
                response_contract=request.response_contract,
                # OMN-15482: thread the three completion-shaping parameters to
                # the dispatch port. Each one stopping here is precisely the
                # silent-drop defect this ticket closes -- pinned by
                # test_handler_propagates_completion_shaping_to_dispatch_port.
                system_prompt=request.system_prompt,
                temperature=request.temperature,
                response_format=request.response_format,
            )
        except Exception as exc:
            return ModelDelegateSkillResponse(
                status="failed",
                correlation_id=request.correlation_id,
                task_type=request.task_type,
                # OMN-14485: a failed delegation still writes a projection row —
                # stamp the resolved tenant so per-tenant failure visibility holds.
                tenant_id=resolved_tenant_id,
                error_message=str(exc),
            )

        return _response_from_result(request, result, tenant_id=resolved_tenant_id)
