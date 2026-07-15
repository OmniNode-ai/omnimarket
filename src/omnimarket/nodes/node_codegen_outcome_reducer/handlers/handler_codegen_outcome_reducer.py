# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outcome-adapter reducer for the hybrid codegen factory (tier-4a.2 / OMN-14608).

THE gap this closes (OMN-14403 G1): the codegen ORCHESTRATOR subscribes to
``codegen-validation-outcome.v1`` / ``codegen-typecheck-outcome.v1`` /
``codegen-serialize-outcome.v1`` (its ``contract.yaml``), but NOTHING in ``src/``
published them. The three pure downstream nodes emit RAW verdicts
(``generated-code-validation-completed.v1`` / ``mypy-check-completed.v1`` /
``contract-serialize-completed.v1``) that carry no pipeline state — their request
models are ``extra="forbid"``, so they cannot echo it. On a real bus the factory
died after the validate command; the golden-chain test only passed because a
test-only ``_DownstreamHarness`` hand-rolled this exact component and doubled
three of six legs (OMN-14208: individually green, silent runtime no-op).

This node is the missing production component. It:

* seeds a per-correlation store from ``codegen-llm-generated.v1`` (the
  state-carrying ``ModelLlmGenerateResult`` — the ONLY input that carries the
  full ``ModelCodegenPipelineState``);
* joins each raw verdict back to that retained state on the correlation key
  (``correlation_id``, threaded through every hop by OMN-14608 and matched
  field-for-field on both sides of the join), and republishes the
  state-carrying ``*Outcome`` event the orchestrator already expects.

Definition B (OMN-14355): ``handle(request: <verdict union>) -> <outcome> | None``
— a bare typed payload in, a bare typed model out, dispatched by ``isinstance``.
It never imports the runtime envelope type (that hard-fails the canon ratchet). The
per-topic ``event_model`` in ``contract.yaml`` (``topic_match``) makes the
dispatcher validate each real producer wire shape before calling ``handle`` —
the same fix pattern OMN-14534 applied to node_swarm_subtask_state_reducer. The
join is 1-in -> 1-out per call (no multi-event fan-out), so it needs neither the
multi-event publish adapter nor the reducer projection adapter.

STATE DURABILITY CAVEAT (honest, load-bearing): the correlation store lives in
this instance's memory. That is replay-proven over a single in-process bus (one
instance subscribed to all four topics, as the golden-chain test drives it), but
it does NOT survive the real runtime, which instantiates one handler per routing
entry — the seed and the verdicts would land in separate instances. Durable
cross-event state (OMN-14208 state_io / the reducer projection adapter,
OMN-14598) is the follow-on that upgrades this from replay-proven to
live-readback. This node deliberately does NOT build that here.
"""

from __future__ import annotations

from omnimarket.codegen.models import (
    ModelCodegenPipelineState,
    ModelCodegenSerializeOutcome,
    ModelCodegenTypecheckOutcome,
    ModelCodegenValidationOutcome,
    ModelGeneratedCodeValidation,
    ModelLlmGenerateResult,
    ModelMypyCheckResult,
)
from omnimarket.contract_assembly.models import ModelContractDocument

# The four real producer wire models this reducer consumes (contract.yaml
# handler_routing event_model per topic) and the three outcomes it emits.
# `reuse — do not fork` (OMN-14403 plan §3 G1): the verdicts are the REAL
# downstream models, imported not mirrored. ModelGeneratedCodeValidation and
# ModelMypyCheckResult are defined in this shared package (not either
# downstream node's private models package) so this cross-node join does not
# reach into a sibling node's internals (OMN-9263 doctrine) — each owning
# node's own models module re-exports the identical class.
ReducerInput = (
    ModelLlmGenerateResult
    | ModelGeneratedCodeValidation
    | ModelMypyCheckResult
    | ModelContractDocument
)
ReducerOutput = (
    ModelCodegenValidationOutcome
    | ModelCodegenTypecheckOutcome
    | ModelCodegenSerializeOutcome
)


def _validation_issues(verdict: ModelGeneratedCodeValidation) -> tuple[str, ...]:
    """Flatten a validator verdict into human-readable rejection reasons.

    These become ``ModelCodegenCompleted.issues`` when the orchestrator emits
    REJECTED_VALIDATION, so they must faithfully reflect the REAL validator's
    findings (syntax errors, empty method bodies, structure mismatches),
    never a synthesized default.
    """
    issues: list[str] = []
    if verdict.syntax_error:
        issues.append(f"syntax error: {verdict.syntax_error}")
    issues.extend(f"stub method: {name}" for name in verdict.stub_methods)
    issues.extend(verdict.structure_issues)
    return tuple(issues)


class HandlerCodegenOutcomeReducer:
    """Join a raw codegen verdict to its retained pipeline state on correlation_id.

    Not a pure ``delta(state, event)``: the runtime hands ``handle`` only the
    event payload (never prior state), so this instance retains the seeded state
    itself. See the module docstring's STATE DURABILITY CAVEAT.
    """

    def __init__(self) -> None:
        # correlation_id -> the run's retained pipeline state (seeded from the
        # llm-generated event, updated when serialize records the contract).
        self._store: dict[str, ModelCodegenPipelineState] = {}

    def handle(self, request: ReducerInput) -> ReducerOutput | None:
        """Route one verdict by its concrete type; None for the seed (no output)."""
        if isinstance(request, ModelLlmGenerateResult):
            # Seed only — the orchestrator drives the validate command off the
            # same llm-generated event, so this leg publishes nothing.
            correlation_id = self._require_correlation_id(request.state.correlation_id)
            self._store[correlation_id] = request.state
            return None

        if isinstance(request, ModelGeneratedCodeValidation):
            state = self._retained(request.correlation_id)
            return ModelCodegenValidationOutcome(
                state=state,
                is_valid=request.is_valid,
                issues=_validation_issues(request),
            )

        if isinstance(request, ModelMypyCheckResult):
            state = self._retained(request.correlation_id)
            return ModelCodegenTypecheckOutcome(
                state=state,
                success=request.success,
                error_count=request.error_count,
            )

        if isinstance(request, ModelContractDocument):
            state = self._retained(request.correlation_id).with_contract(
                request.contract_yaml
            )
            # The serialize verdict advances the state (contract_yaml now set);
            # retain it so a later observer of the same run sees the latest.
            self._store[request.correlation_id] = state
            return ModelCodegenSerializeOutcome(state=state)

        raise TypeError(  # pragma: no cover - guarded by contract event_model wiring
            "HandlerCodegenOutcomeReducer.handle() received unrecognized payload "
            f"type {type(request).__name__!r}; expected one of ModelLlmGenerateResult, "
            "ModelGeneratedCodeValidation, ModelMypyCheckResult, ModelContractDocument."
        )

    def _retained(self, correlation_id: str) -> ModelCodegenPipelineState:
        """Return the seeded state for ``correlation_id`` or fail loud.

        A verdict for a correlation this reducer never saw an llm-generated seed
        for is a wiring defect (out-of-order delivery or a lost seed), not a
        degradable condition — fail rather than fabricate empty state.
        """
        correlation_id = self._require_correlation_id(correlation_id)
        state = self._store.get(correlation_id)
        if state is None:
            raise ValueError(
                "HandlerCodegenOutcomeReducer: no retained pipeline state for "
                f"correlation_id={correlation_id!r} — a verdict arrived without a "
                "prior codegen-llm-generated seed (wiring defect or lost seed)."
            )
        return state

    @staticmethod
    def _require_correlation_id(correlation_id: str) -> str:
        """Fail loud on a blank ``correlation_id`` rather than a silent join.

        The reducer-facing seed/verdict models default ``correlation_id`` to
        ``""`` (it's an additive OMN-14608 field on models that pre-date the
        reducer). Treating that default as a valid ``_store`` key would let
        unrelated, uncorrelated runs collide on the same key instead of
        failing loudly on what is actually a wiring defect (a producer that
        never propagated the id).
        """
        if not correlation_id:
            raise ValueError(
                "HandlerCodegenOutcomeReducer: blank correlation_id — the "
                "upstream producer did not propagate it (wiring defect)."
            )
        return correlation_id


__all__ = ["HandlerCodegenOutcomeReducer"]
