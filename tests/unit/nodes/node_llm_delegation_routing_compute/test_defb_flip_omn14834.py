# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14834 — canonical def-B flip proof for node_llm_delegation_routing_compute.

RED-against-EXISTS-but-WRONG + behavior-equivalence proof for the hand-flip
(OMN-14781) of ``HandlerDelegationRouting`` from def-A to canonical def-B.

Before this ticket ``HandlerDelegationRouting.handle`` had the multi-positional
signature ``handle(self, correlation_id, input_data)`` — two positional params,
which the canonical-shape ratchet classifies ``nonadaptable`` and which the shared
``RuntimeLocal`` adapter cannot dispatch: the runtime invokes ``handle`` with a
SINGLE positional payload (``runtime_local.py`` ``_invoke_handler_method`` docstring:
"handle() receives a single positional payload argument"), so the two-argument
entrypoint raises ``TypeError: handle() missing 1 required positional argument:
'input_data'`` on the FIRST dispatch (RuntimeLocal-undispatchable, OMN-13711). The
``correlation_id`` parameter was UNUSED.

The flip is a pure signature adaptation: ``handle(self, request)`` reads the same
``ModelDelegationRoutingInput`` and calls the SAME ``_select_model`` helper. The
business-logic symbols (``_select_model`` / ``_estimate_tokens`` / ``_is_degraded`` /
``_is_unhealthy``) are byte-identical base_ref<->HEAD, which the canonical-shape
ratchet re-derives from git (the ``.handflip.json`` proof).

Tests here:
  * ``test_handle_is_single_payload_adaptable`` — drives the SINGLE-payload dispatch
    shape the runtime uses; RED on the def-A handler (TypeError), GREEN on def-B.
  * ``test_multipositional_handle_is_the_red`` — an inline def-A-shaped stub proves the
    single-payload dispatch oracle actually discriminates (raises TypeError), so the
    test above is not vacuously green.
  * ``test_defb_output_matches_recorded_golden`` — byte/sha256 behavior-equivalence over
    the recorded corpus (durable regression lock, OMN-14589 style).
  * ``test_golden_fingerprint_discriminates`` — a one-field perturbation flips the
    fingerprint, proving the golden oracle is non-vacuous.

Every corpus input is ``now``-independent (degradation ``expires_at`` is year 2999 =
always-active or year 2000 = always-expired), so the handler's internal
``datetime.now(tz=UTC)`` cannot make the recorded output flaky.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_request import (
    ModelLlmDelegationRequest,
)
from omnimarket.models.delegation.llm_cost_routing.model_routing_policy import (
    ModelDelegationModelProfile,
    ModelDelegationRoutingPolicy,
    ModelDelegationTaskPolicy,
)
from omnimarket.nodes.node_llm_delegation_routing_compute.handlers.handler_delegation_routing import (
    HandlerDelegationRouting,
)
from omnimarket.nodes.node_llm_delegation_routing_compute.models.model_delegation_routing_input import (
    DegradationEntry,
    HealthEntry,
    ModelDelegationRoutingInput,
)
from omnimarket.nodes.node_llm_delegation_routing_compute.models.model_delegation_routing_output import (
    ModelDelegationRoutingOutput,
)

pytestmark = pytest.mark.unit

_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

# Always-future / always-past relative to any realistic wall clock -> now-independent.
from datetime import UTC, datetime  # noqa: E402

_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)
_PAST = datetime(2000, 1, 1, tzinfo=UTC)


def _profile(
    model_id: str,
    tier: str = "local",
    endpoint_env: str | None = None,
    max_context: int = 100_000,
    cost_basis: EnumCostBasis = EnumCostBasis.ZERO_MARGINAL_API_COST,
    provider: str = "local",
    cost_in: str = "0",
    cost_out: str = "0",
) -> ModelDelegationModelProfile:
    return ModelDelegationModelProfile(
        model_id=model_id,
        endpoint_env=endpoint_env or f"LLM_{model_id}_URL",
        provider=provider,
        tier=tier,
        cost_per_1m_input=Decimal(cost_in),
        cost_per_1m_output=Decimal(cost_out),
        cost_basis=cost_basis,
        max_context=max_context,
    )


def _policy(
    preferred: list[str] | None = None,
    fallback: str = "model-c",
    extra_profiles: dict[str, ModelDelegationModelProfile] | None = None,
) -> ModelDelegationRoutingPolicy:
    profiles: dict[str, ModelDelegationModelProfile] = {
        "model-a": _profile("model-a", endpoint_env="LLM_A_URL"),
        "model-b": _profile("model-b", endpoint_env="LLM_B_URL"),
        "model-c": _profile("model-c", endpoint_env="LLM_C_URL"),
    }
    if extra_profiles:
        profiles.update(extra_profiles)
    return ModelDelegationRoutingPolicy(
        version="0.1.0",
        pricing_manifest_version="0.1.0",
        task_policies={
            "changelog": ModelDelegationTaskPolicy(
                task_type="changelog",
                preferred_models=preferred or ["model-a", "model-b"],
                fallback=fallback,
                max_tokens=1024,
                temperature=0.3,
            )
        },
        model_profiles=profiles,
    )


def _request(
    task_type: str = "changelog",
    prompt: str = "short prompt",
    required_tier: str | None = None,
) -> ModelLlmDelegationRequest:
    return ModelLlmDelegationRequest(
        task_type=task_type,
        prompt_hash="abc123",
        prompt=prompt,
        required_tier=required_tier,
    )


def _input(
    request: ModelLlmDelegationRequest | None = None,
    policy: ModelDelegationRoutingPolicy | None = None,
    degradation_state: dict[tuple[str, str], DegradationEntry] | None = None,
    health_state: dict[str, HealthEntry] | None = None,
) -> ModelDelegationRoutingInput:
    return ModelDelegationRoutingInput(
        request=request or _request(),
        policy=policy or _policy(),
        degradation_state=degradation_state or {},
        health_state=health_state or {},
    )


def _frontier() -> ModelDelegationModelProfile:
    return _profile(
        "frontier-model",
        tier="frontier",
        endpoint_env="LLM_FRONTIER_URL",
        max_context=200_000,
        cost_basis=EnumCostBasis.CLOUD_API_COST,
        provider="anthropic",
        cost_in="3.0",
        cost_out="15.0",
    )


def _deg(expires: datetime, reason: str = "quality gate failure") -> DegradationEntry:
    return DegradationEntry(expires_at=expires, reason=reason)


def _health(healthy: bool, has_capacity: bool) -> HealthEntry:
    return HealthEntry(healthy=healthy, has_capacity=has_capacity, checked_at=_PAST)


def build_corpus() -> list[tuple[str, ModelDelegationRoutingInput]]:
    """Deterministic, ``now``-independent corpus exercising every routing branch.

    Cases whose id ends in ``_raises`` deterministically raise ``ValueError`` in
    ``_select_model``; the golden records the error class + message for them.
    """
    _big_b = _profile("model-b", endpoint_env="LLM_B_URL", max_context=200_000)
    _pol_big_b = _policy(extra_profiles={"model-b": _big_b})
    long_prompt = "x" * 320_004  # 80_001 est tokens > 80% of model-a's 100k context
    at_limit_prompt = "x" * 320_000  # exactly 80_000 est tokens == threshold (not >)

    return [
        ("happy_first_preferred", _input()),
        (
            "active_degradation_skips_a",
            _input(degradation_state={("changelog", "model-a"): _deg(_FUTURE)}),
        ),
        (
            "expired_degradation_ignored",
            _input(degradation_state={("changelog", "model-a"): _deg(_PAST)}),
        ),
        (
            "context_overflow_skips_a",
            _input(request=_request(prompt=long_prompt), policy=_pol_big_b),
        ),
        (
            "context_at_limit_not_skipped",
            _input(request=_request(prompt=at_limit_prompt)),
        ),
        (
            "unhealthy_a_skips",
            _input(health_state={"LLM_A_URL": _health(False, True)}),
        ),
        (
            "no_capacity_a_skips",
            _input(health_state={"LLM_A_URL": _health(True, False)}),
        ),
        (
            "all_preferred_degraded_fallback",
            _input(
                degradation_state={
                    ("changelog", "model-a"): _deg(_FUTURE),
                    ("changelog", "model-b"): _deg(_FUTURE),
                }
            ),
        ),
        (
            "tier_override_frontier",
            _input(
                request=_request(required_tier="frontier"),
                policy=_policy(extra_profiles={"frontier-model": _frontier()}),
            ),
        ),
        (
            "multi_skip_degraded_a_unhealthy_b",
            _input(
                degradation_state={("changelog", "model-a"): _deg(_FUTURE)},
                health_state={"LLM_B_URL": _health(False, True)},
            ),
        ),
        (
            "degraded_fallback_raises",
            _input(
                degradation_state={
                    ("changelog", "model-a"): _deg(_FUTURE),
                    ("changelog", "model-b"): _deg(_FUTURE),
                    ("changelog", "model-c"): _deg(_FUTURE),
                }
            ),
        ),
        (
            "unhealthy_fallback_raises",
            _input(
                degradation_state={
                    ("changelog", "model-a"): _deg(_FUTURE),
                    ("changelog", "model-b"): _deg(_FUTURE),
                },
                health_state={"LLM_C_URL": _health(False, True)},
            ),
        ),
        (
            "tier_no_match_raises",
            _input(request=_request(required_tier="premium")),
        ),
        (
            "tier_all_skipped_raises",
            _input(
                request=_request(required_tier="frontier"),
                policy=_policy(extra_profiles={"frontier-model": _frontier()}),
                degradation_state={("changelog", "frontier-model"): _deg(_FUTURE)},
            ),
        ),
        (
            "unknown_task_type_raises",
            _input(request=_request(task_type="nonexistent_task")),
        ),
    ]


def _run(inp: ModelDelegationRoutingInput) -> ModelDelegationRoutingOutput:
    return asyncio.run(HandlerDelegationRouting().handle(inp))


def canonical_result(inp: ModelDelegationRoutingInput) -> dict[str, object]:
    """Byte-canonical record of the def-B handler's observable behavior for ``inp``."""
    try:
        out = _run(inp)
    except ValueError as exc:
        return {"kind": "error", "error_type": "ValueError", "message": str(exc)}
    return {"kind": "ok", "output": out.model_dump(mode="json")}


def fingerprint(result: dict[str, object]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(result, sort_keys=True).encode("utf-8")).hexdigest()
    )


def input_fingerprint(inp: ModelDelegationRoutingInput) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(inp.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        ).hexdigest()
    )


_CORPUS = build_corpus()


# --------------------------------------------------------------------------- #
# Dispatch-shape RED->GREEN (single positional payload is the runtime contract)
# --------------------------------------------------------------------------- #


def test_handle_is_single_payload_adaptable() -> None:
    """The runtime calls handle(payload) with ONE positional arg — def-B satisfies it.

    RED on the pre-flip def-A ``handle(self, correlation_id, input_data)`` (raises
    ``TypeError: missing 1 required positional argument: 'input_data'``); GREEN on the
    def-B ``handle(self, request)``.
    """
    out = asyncio.run(HandlerDelegationRouting().handle(_input()))
    assert isinstance(out, ModelDelegationRoutingOutput)
    assert out.selection.model_id == "model-a"


def test_multipositional_handle_is_the_red() -> None:
    """A def-A-shaped stub proves single-payload dispatch discriminates (not vacuous)."""

    class _DefAStub:
        async def handle(
            self,
            correlation_id: object,
            input_data: object,
        ) -> None:  # pragma: no cover - only its arity is under test
            return None

    with pytest.raises(TypeError):
        asyncio.run(_DefAStub().handle(_input()))  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Behavior-equivalence golden (durable regression lock)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("case_id", "inp"), _CORPUS, ids=[c for c, _ in _CORPUS])
def test_defb_output_matches_recorded_golden(
    case_id: str, inp: ModelDelegationRoutingInput
) -> None:
    golden_path = _GOLDEN_DIR / f"{case_id}.json"
    assert golden_path.exists(), (
        f"missing golden {golden_path}; regenerate with the OMN-14834 recorder"
    )
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    live = canonical_result(inp)
    assert input_fingerprint(inp) == golden["input_hash"], (
        f"input fingerprint drift for {case_id}"
    )
    assert live == golden["result"], f"behavior drift for {case_id}"
    assert fingerprint(live) == golden["fingerprint"], (
        f"fingerprint drift for {case_id}"
    )


def test_golden_fingerprint_discriminates() -> None:
    """A one-field perturbation of a recorded result changes the fingerprint."""
    inp = _input()
    base = canonical_result(inp)
    assert base["kind"] == "ok"
    perturbed = json.loads(json.dumps(base))
    perturbed["output"]["selection"]["model_id"] = "model-PERTURBED"
    assert fingerprint(perturbed) != fingerprint(base)
