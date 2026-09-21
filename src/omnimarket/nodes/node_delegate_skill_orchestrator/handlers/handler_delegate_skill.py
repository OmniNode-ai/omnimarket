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

import asyncio
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from omnibase_core.models.delegation.wire import (
    EnumDelegationTerminalFailureCause,
    EnumQualityScoreComparison,
    ModelDelegationProvenance,
    ModelPremiumCounterfactual,
)
from omnibase_infra.runtime.dispatch_envelope_context import (
    current_dispatch_envelope,
)
from pydantic import ValidationError

from omnimarket.config import get_settings
from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.local_deployment.tenant_identity import (
    local_tenant_identity_or_none,
)
from omnimarket.models.delegation.credential_withheld_rung import (
    ModelCredentialWithheldRung,
)
from omnimarket.models.delegation.local_credential_refusal import (
    ModelLocalCredentialRefusal,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
    ModelDelegateSkillResponseMetrics,
    delegate_skill_terminal_from_response,
    resolve_terminal_failure_cause,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_handler_execution_budget import (
    ModelDelegateSkillHandlerBudget,
    load_handler_execution_budget,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_delegation_claim import (
    ProtocolDelegationIdempotencyPort,
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
        provenance: ModelDelegationProvenance | None = None,
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


def _as_acceptance_decision(
    value: object,
) -> EnumDelegationAcceptanceDecision | None:
    """Coerce a serialized accept/climb decision (OMN-16932).

    ``None`` for an attempt record that carries no decision — a transport skip,
    or a record written before the field existed. Unknown values are treated as
    absent rather than raising: this is a read-side projection of history, and a
    terminal must still render when an older row carries a value this build does
    not know.
    """
    if value is None:
        return None
    if isinstance(value, EnumDelegationAcceptanceDecision):
        return value
    if isinstance(value, str):
        try:
            return EnumDelegationAcceptanceDecision(value)
        except ValueError:
            return None
    return None


def _as_acceptance_reason(
    value: object,
) -> EnumDelegationAcceptanceReason | None:
    """Coerce a serialized accept/climb reason (OMN-16932). See above."""
    if value is None:
        return None
    if isinstance(value, EnumDelegationAcceptanceReason):
        return value
    if isinstance(value, str):
        try:
            return EnumDelegationAcceptanceReason(value)
        except ValueError:
            return None
    return None


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


logger = logging.getLogger(__name__)


def _as_credential_refusal(value: object) -> ModelLocalCredentialRefusal | None:
    """Parse the port's typed credential refusal, or ``None`` when absent.

    OMN-18696. Accepts the model itself (the in-process port) and its dumped
    mapping (anything that crossed a serialization boundary). A mapping that
    does not validate returns ``None``: the prose refusal on ``error_message``
    still reaches the caller, and no field of a refusal is ever guessed.
    """
    if value is None:
        return None
    if isinstance(value, ModelLocalCredentialRefusal):
        return value
    if isinstance(value, Mapping):
        try:
            return ModelLocalCredentialRefusal.model_validate(dict(value))
        except ValidationError:
            logger.warning(
                "OMN-18696: dropping an unparseable credential_refusal payload"
            )
            return None
    return None


def _as_credential_withheld(value: object) -> ModelCredentialWithheldRung | None:
    """Parse the port's withheld-rung fact, or ``None`` when absent.

    OMN-18696 second pass, and deliberately a separate parser from
    ``_as_credential_refusal`` rather than a generic one: the two payloads mean
    different things and must not be able to validate into each other's field.
    An unparseable mapping is dropped on the same terms -- no field of a
    credential fact is ever guessed.
    """
    if value is None:
        return None
    if isinstance(value, ModelCredentialWithheldRung):
        return value
    if isinstance(value, Mapping):
        try:
            return ModelCredentialWithheldRung.model_validate(dict(value))
        except ValidationError:
            logger.warning(
                "OMN-18696: dropping an unparseable credential_withheld payload"
            )
            return None
    return None


def _as_secret_source(value: object) -> EnumSecretSource | None:
    """Coerce a terminal's ``secret_source`` to the enum, or ``None``.

    An unrecognised value resolves to ``None`` rather than raising: a receipt
    is evidence about a delegation that already happened, and refusing to build
    it over an unreadable provenance field would lose the delegation's own
    result in order to report a bookkeeping fault.
    """
    if isinstance(value, EnumSecretSource):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return EnumSecretSource(value)
    except ValueError:
        return None


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


def _as_optional_int(value: object) -> int | None:
    """Coerce a dispatch-port integer field, preserving an absent value as None.

    OMN-18297: ``None`` here means the comparison did not happen on this rung,
    which is a different fact from a measurement of zero.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _attempt_records(
    result: dict[str, object],
) -> list[ModelDelegateSkillAttemptRecord]:
    """Build the typed per-tier attempt ladder (OMN-14063).

    ``result["attempts"]`` is the richer dispatch-port-owned record and always
    wins when present. Canonical bus terminals currently expose only serialized
    ``escalation_history``; map that as documented best-effort evidence rather
    than dropping it. History may lack backend aliases, so callers must use the
    separate ``attempts_count`` as the total-call authority.

    OMN-16932: history is no longer rejections-only — the ACCEPTED rung is
    recorded there too, so that the rung which answered is legible rather than
    inferable. Each row's typed ``acceptance_decision`` is therefore the
    authority for whether it passed; a row without one predates the field and
    keeps the old rejected-only reading.
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
            decision = _as_acceptance_decision(raw.get("acceptance_decision"))
            records.append(
                ModelDelegateSkillAttemptRecord(
                    tier=str(raw.get("tier_name") or raw.get("tier") or ""),
                    backend_id=str(
                        raw.get("backend_id") or raw.get("routing_decision_id") or ""
                    ),
                    model_id=str(raw.get("model_used") or raw.get("model_id") or ""),
                    # OMN-16932: escalation history used to hold ONLY rejected
                    # attempts, so this was hardcoded False. The accepted rung is
                    # now recorded there too — that is the whole point of the
                    # ticket, the winning rung has to be legible — so a hardcoded
                    # False would relabel the attempt that ANSWERED as a failure.
                    # The typed decision is the authority; absent it (a record
                    # predating this field) the old rejected-only reading holds.
                    quality_gate_passed=(
                        decision is EnumDelegationAcceptanceDecision.ACCEPT
                    ),
                    acceptance_decision=decision,
                    acceptance_reason=_as_acceptance_reason(
                        raw.get("acceptance_reason")
                    ),
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
                # OMN-16932: the dispatch-port path records the same typed
                # accept/climb verdict the bus path does, so a reader of either
                # terminal sees WHY a rung was kept or abandoned.
                acceptance_decision=_as_acceptance_decision(
                    raw.get("acceptance_decision")
                ),
                acceptance_reason=_as_acceptance_reason(raw.get("acceptance_reason")),
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
                # OMN-18297: the budget comparison, when one was performed.
                input_tokens_measured=_as_optional_int(
                    raw.get("input_tokens_measured")
                ),
                input_token_budget=_as_optional_int(raw.get("input_token_budget")),
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


def _elapsed_ms(started_monotonic: float) -> int:
    """Return whole milliseconds elapsed since a monotonic start reading."""
    return max(0, int((time.monotonic() - started_monotonic) * 1000.0))


def _queue_wait_ms(
    request: ModelDelegateSkillRequest, picked_up_at: datetime
) -> int | None:
    """Return publish-to-pickup milliseconds, or None when it was not measured.

    OMN-18852. ``published_at`` is stamped by the producer; a request without
    it yields ``None``, never ``0`` -- an unstamped request and an empty queue
    are different facts and must not render the same. A negative interval is
    producer/consumer clock skew rather than a negative wait, and is floored at
    zero: the measurement still happened, it is simply bounded below.
    """
    if request.published_at is None:
        return None
    delta_ms = (picked_up_at - request.published_at).total_seconds() * 1000.0
    return max(0, int(delta_ms))


def _response_from_result(
    request: ModelDelegateSkillRequest,
    result: dict[str, object],
    *,
    tenant_id: str | None,
    queue_wait_ms: int | None,
    execution_duration_ms: int,
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
    attempts = _attempt_records(result)
    # OMN-15469: classify the terminal failure cause from the ladder BEFORE the
    # response is built, so the composite verdict (delegate_skill_succeeded) can
    # see it. A quota refusal that reaches here unclassified is the exact case
    # that used to terminalize as a success.
    terminal_failure_cause = resolve_terminal_failure_cause(
        attempts, error_message=error_message
    )
    explicit_terminal_failure_cause = _as_terminal_failure_cause(
        result.get("terminal_failure_cause")
    )
    terminal_failure_cause = explicit_terminal_failure_cause or terminal_failure_cause
    score_vs_required_bar = _as_quality_score_comparison(
        result.get("score_vs_required_bar")
    )
    failed_acceptance_criteria = tuple(
        _as_str_list(result.get("failed_acceptance_criteria"))
    )
    if terminal_failure_cause is not None:
        quality_gate_passed = False
        # OMN-17979: the downgrade above is what makes a quality-failed response
        # at or above its bar, and the wire model REQUIRES such a response to
        # name the criterion it failed. Pre-fix it named nothing, so the
        # response could not be constructed and the command terminalized as an
        # auto-wiring boundary failure instead of a delegation terminal. The
        # criterion is stated from the typed cause plus the evidence actually
        # observed -- the validator's premise satisfied with a real reason, not
        # relaxed and not filled with a placeholder.
        if (
            score_vs_required_bar is EnumQualityScoreComparison.AT_OR_ABOVE_BAR
            and not failed_acceptance_criteria
        ):
            observed = error_message or "; ".join(
                attempt.error_message for attempt in attempts if attempt.error_message
            )
            criterion = f"terminal_failure_cause:{terminal_failure_cause.value}"
            failed_acceptance_criteria = (
                f"{criterion} ({observed})" if observed else criterion,
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
    return ModelDelegateSkillResponse(
        status=status_value,
        correlation_id=request.correlation_id,
        task_type=request.task_type,
        # OMN-14485: carry the resolved tenant onto the response so the terminal
        # event this becomes stamps a real tenant on the projection row.
        tenant_id=tenant_id,
        provenance=request.provenance,
        # OMN-17013 (DR-02): bind the receipt to a provider IDENTITY, never to the
        # address the rung was reached at. The bus dispatch port returns the parsed
        # terminal — an omnibase_core ModelDelegationCompleted/Failed payload — whose
        # declared ``provider`` (added by OMN-18079, omnibase_core#1675) is the
        # routing authority's own answer for who served the call. That model has
        # never carried ``delegated_to``; only the LOCAL in-process port hand-builds
        # that key, which is why reading it first left every BUS receipt falling
        # through to ``endpoint_url`` — a LAN address for local rungs — while the
        # real identity sat unread in the same payload.
        #
        # ``endpoint_url`` is deliberately NOT a fallback here. It is always present
        # on the wire, so keeping it would mean the absent-identity case is
        # indistinguishable from a resolved one for every downstream cost
        # attribution and provenance audit. An unrouted terminal drops ``provider``
        # entirely (the field carries ``exclude_if`` on None), and an explicit empty
        # is the honest rendering of that: the runtime resolves identity, the
        # receipt reports what it resolved.
        # OMN-18695: the credential's provenance, carried through unchanged.
        # Absent stays absent -- an unauthenticated local backend records no
        # source rather than a default one, so "resolved from the store" on a
        # receipt is always something that was observed.
        secret_source=_as_secret_source(result.get("secret_source")),
        secret_ref=(str(result["secret_ref"]) if result.get("secret_ref") else None),
        provider=str(result.get("provider") or result.get("delegated_to") or ""),
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
        score_vs_required_bar=score_vs_required_bar,
        failed_acceptance_criteria=failed_acceptance_criteria,
        terminal_failure_cause=terminal_failure_cause,
        quality_gates_failed=quality_failures,
        # OMN-18696: parsed, never reconstructed. A malformed payload is dropped
        # rather than coerced -- a refusal that names the wrong credential is
        # worse than one the caller has to read out of ``error_message``.
        credential_refusal=_as_credential_refusal(result.get("credential_refusal")),
        # OMN-18696 (second pass): parsed the same way and kept on its own
        # field. See the wire model for why this is not folded into the one
        # above.
        credential_withheld=_as_credential_withheld(result.get("credential_withheld")),
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
        # OMN-18852: queue and execution as separate terminal facts. The
        # dispatch port reports neither -- both are measured by the handler,
        # which is the only party that knows when it picked the record up.
        queue_wait_ms=queue_wait_ms,
        execution_duration_ms=execution_duration_ms,
    )


class HandlerDelegateSkill:
    """Translate a typed delegation request to a runtime command via the port."""

    def __init__(
        self,
        event_bus: ProtocolDelegationEventBus | None = None,
        *,
        dispatch_port: ProtocolDelegationDispatchPort | None = None,
        idempotency_port: ProtocolDelegationIdempotencyPort | None = None,
        budget: ModelDelegateSkillHandlerBudget | None = None,
    ) -> None:
        # OMN-15504: the wall-clock bound this handler enforces on itself. It is
        # contract-declared, never a default here: an invisible constant
        # governing a consumer eviction deadline is the shape that produced the
        # live livelock in the first place.
        self._budget = budget if budget is not None else load_handler_execution_budget()
        # OMN-18887: the correlation-keyed claim. Injected on the same terms as
        # the dispatch port and resolved by the same ports package, so this
        # handler owns a protocol rather than a database, and every one of the
        # four construction sites gets the check without being touched.
        if idempotency_port is not None:
            self._idempotency_port: ProtocolDelegationIdempotencyPort | None = (
                idempotency_port
            )
        else:
            from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_selection import (
                select_delegation_idempotency_port,
            )

            self._idempotency_port = select_delegation_idempotency_port()
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

    async def _dispatch_and_build_terminal(
        self, request: ModelDelegateSkillRequest
    ) -> ModelDelegateSkillCompleted | ModelDelegateSkillFailed:
        """Dispatch the request and return the typed TERMINAL variant.

        On any dispatch exception, returns ``status="failed"`` with the error text
        rather than propagating — failed delegations must remain observable.

        OMN-15469: the return value is the contract's ONE terminal for this
        command (this handler self-publishes nothing; the definition-B wiring
        owns publication). Which of the contract's two declared terminal events
        it becomes is selected by the returned CLASS —
        ``ModelDelegateSkillCompleted`` vs ``ModelDelegateSkillFailed``, both
        declared in the contract. A bare ``ModelDelegateSkillResponse`` matches
        neither declaration, and the wiring then falls back to the contract's
        SUCCESS terminal, which is how a command whose every attempt was
        refused with HTTP 429 terminalized as completed and reported
        ``ok=true`` to the caller.
        """
        # OMN-14485: resolve the tenant identity ONCE at request-acceptance and
        # carry it onto the response (and thus the auto-published terminal event
        # node_projection_delegation reads). Precedence mirrors the local dispatch
        # port and HandlerDelegationWorkflow: a verified request-carried tenant_id
        # wins; otherwise the ONEX_TENANT_ID interim (OMN-14058) applies; else None.
        # The dispatch port still receives the verified request tenant_id (OMN-14349
        # seam) — the env-var interim is a projection-stamping fallback, not a
        # verified-identity source at the port boundary.
        #
        # OMN-18699 adds the last step, and only the last step: this install's
        # OWN minted identity, read from the local store by reference. This
        # handler serves BOTH the bus and the bus-less local path, so the reader
        # is the non-raising one -- a bus runtime has no local store, reads
        # None, and behaves exactly as it did before. It is still never the
        # house constant. Without this step a local run that HAD minted an
        # identity would have its evidence row stamped with it (the port
        # resolves the same identity) while its RECEIPT carried None, which is
        # the split AC "receipts carry the minted identity" exists to refuse.
        resolved_tenant_id = (
            request.tenant_id
            or get_settings().onex_tenant_id
            or local_tenant_identity_or_none()
        )
        # OMN-18852: PICKUP is here, and the budget below is measured from
        # here -- not from the record's publish time. That was already true
        # before this change (``asyncio.wait_for`` starts when ``handle`` is
        # entered) and is now stated rather than inferred, because the two
        # intervals are reported separately on the terminal and a reader has
        # to know which one the budget governs. The monotonic clock times the
        # work; the wall clock is only for the interval against the producer's
        # own stamp, which lives in another process.
        picked_up_monotonic = time.monotonic()
        queue_wait_ms = _queue_wait_ms(request, datetime.now(UTC))
        try:
            # OMN-15504: bound the AWAIT, not merely the code around it. The
            # dispatch is a single await, so there is no loop body in which a
            # deadline could be re-checked -- a bound expressed anywhere but
            # here would never be evaluated once the port stopped resolving.
            # asyncio.wait_for also CANCELS the dispatch on expiry rather than
            # orphaning it, which matters because an abandoned dispatch keeps
            # the runtime port's correlation-scoped broker subscription open
            # and hands the same stall to the next record.
            result = await asyncio.wait_for(
                self._dispatch_port.dispatch(
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
                    # OMN-18172: carry the canonical typed classifier into the
                    # selected dispatch path. None remains explicit legacy /
                    # unclassified provenance and is never promoted to synthetic.
                    provenance=request.provenance,
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
                ),
                timeout=float(self._budget.max_handler_duration_seconds),
            )
        except TimeoutError:
            # OMN-15504: the handler's own budget expired. This is deliberately
            # NOT routed through resolve_terminal_failure_cause(): that helper
            # classifies what the PROVIDER reported, and its step 3 turns any
            # outer error text into `provider_error`. No provider reported
            # anything here -- we stopped waiting. Attributing our own budget to
            # the provider is precisely the misattribution OMN-16998 removed
            # from this field, and it would feed a failure the provider never
            # had into the over-quota metric measured from it. `status="timeout"`
            # is a declared terminal status and carries the fact without
            # inventing a cause.
            budget_seconds = self._budget.max_handler_duration_seconds
            # OMN-18852: report the queue wait alongside the budget when it was
            # measured. "Exceeded the 240 s budget" is the same sentence for a
            # job that genuinely ran 240 s and for one that sat 445 s in a
            # queue and then ran 3 s, and the remedies are opposite. When the
            # producer stamped nothing the clause is omitted entirely rather
            # than reported as zero -- the refusal states what was observed.
            queue_clause = (
                ""
                if queue_wait_ms is None
                else (
                    f"; measured queue wait before pickup: {queue_wait_ms} ms "
                    "(the budget is measured from pickup, not from publish)"
                )
            )
            return ModelDelegateSkillFailed(
                status="timeout",
                correlation_id=request.correlation_id,
                task_type=request.task_type,
                tenant_id=resolved_tenant_id,
                provenance=request.provenance,
                error_message=(
                    f"delegation exceeded the handler execution budget of "
                    f"{budget_seconds}s and was cancelled; the consumer commits "
                    "this terminal instead of being evicted mid-handle "
                    f"(OMN-15504){queue_clause}"
                ),
                terminal_failure_cause=None,
                queue_wait_ms=queue_wait_ms,
                execution_duration_ms=_elapsed_ms(picked_up_monotonic),
            )
        except Exception as exc:
            return ModelDelegateSkillFailed(
                status="failed",
                correlation_id=request.correlation_id,
                task_type=request.task_type,
                # OMN-14485: a failed delegation still writes a projection row —
                # stamp the resolved tenant so per-tenant failure visibility holds.
                tenant_id=resolved_tenant_id,
                provenance=request.provenance,
                error_message=str(exc),
                queue_wait_ms=queue_wait_ms,
                execution_duration_ms=_elapsed_ms(picked_up_monotonic),
                # OMN-15469: a dispatch exception is a failure terminal, so it
                # must carry the FAILED class identity. Returning the base
                # response here routed hard dispatch failures onto the SUCCESS
                # terminal by map-miss fallback.
                terminal_failure_cause=resolve_terminal_failure_cause(
                    (), error_message=str(exc)
                ),
            )

        return delegate_skill_terminal_from_response(
            _response_from_result(
                request,
                result,
                tenant_id=resolved_tenant_id,
                queue_wait_ms=queue_wait_ms,
                execution_duration_ms=_elapsed_ms(picked_up_monotonic),
            )
        )

    @staticmethod
    def _terminal_from_record(
        record: dict[str, object],
    ) -> ModelDelegateSkillCompleted | ModelDelegateSkillFailed | None:
        """Rebuild a previously served terminal, or return None if it cannot be.

        None here means "fall through and dispatch". That is the safe
        direction: re-running costs money once more, whereas handing back a
        terminal we could not faithfully rebuild would answer the caller with
        something we made up.
        """
        cls_name = record.get("cls")
        data = record.get("data")
        if not isinstance(data, dict):
            return None
        for candidate in (ModelDelegateSkillCompleted, ModelDelegateSkillFailed):
            if cls_name != candidate.__name__:
                continue
            try:
                return candidate.model_validate(data)
            except Exception:  # a stored row we cannot parse is not a terminal
                return None
        return None

    async def handle(
        self, request: ModelDelegateSkillRequest
    ) -> ModelDelegateSkillCompleted | ModelDelegateSkillFailed:
        """Claim this correlation, then dispatch it at most once (OMN-18887).

        The consume path auto-commits and never calls ``commit()``, so delivery
        is at-least-once by contract, and since OMN-18852 four records run in
        flight at once. Without a claim, a rebalance, a crash or a rewind
        re-runs the delegation end to end: a fresh inference, a second provider
        call, a second billing row, and nothing failing to show for it.

        Three properties, in the order they matter:

        * the claim runs BEFORE dispatch, so the provider is never called twice
          for one correlation;
        * a lost claim still ANSWERS -- it returns the terminal the first run
          recorded, never ``None``. A ``None`` result publishes no terminal at
          all, which would convert a double-bill into the missing-envelope
          defect OMN-15504 exists to prevent;
        * the claim is durable, so the redelivery cause that matters most, a
          crash, is covered. In-process memoisation would not be.

        Residual, stated rather than implied: a redelivery arriving while the
        first attempt is still IN FLIGHT loses the claim but finds no recorded
        terminal yet, and falls through to dispatch. That is the conservative
        direction -- it costs one more inference rather than answering with a
        terminal that does not exist -- and closing it needs the first run to
        publish an in-flight marker the second can wait on, which is a
        different change from this one.
        """
        port = self._idempotency_port
        if port is None:
            return await self._dispatch_and_build_terminal(request)

        # OMN-18887: the claim keys on the DELIVERING RECORD, which the runtime
        # binds around this dispatch. After OMN-18958 that envelope's id IS the
        # wire message id, so a redelivery of one record carries the same value
        # and a genuinely new command carries a different one -- even when a
        # caller reuses the correlation, which callers do.
        #
        # No bound envelope means no delivery: a direct call, the bus-less CLI,
        # or another handler composing this one. None of those is a redelivery,
        # so there is nothing to suppress and the claim is skipped rather than
        # faked against a substitute key.
        delivery = current_dispatch_envelope()
        if delivery is None:
            return await self._dispatch_and_build_terminal(request)
        delivery_id = delivery.envelope_id

        # No tenant is resolved for the claim, deliberately. The claim row is
        # an omninode_internal relation, which receives no tenant stamping and
        # no row-level security, so a tenant column there would be a posture
        # the schema cannot enforce. The claim keys on the delivering record,
        # which is tenant-agnostic anyway.
        outcome = port.claim(
            delivery_id=delivery_id,
            correlation_id=request.correlation_id,
        )
        if not outcome.won and outcome.served_terminal is not None:
            replayed = self._terminal_from_record(outcome.served_terminal)
            if replayed is not None:
                return replayed

        terminal = await self._dispatch_and_build_terminal(request)
        port.record_terminal(
            delivery_id=delivery_id,
            terminal={
                "cls": type(terminal).__name__,
                "data": terminal.model_dump(mode="json"),
            },
        )
        return terminal
