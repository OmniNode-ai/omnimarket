# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20172: on a rubric failure a task is swapped, shrunk on the same model, then escalated.

The ladder (plan 13.6 and 13.10): one configuration swap on the same model and
backend, then shrink (split, units on the same model and backend, recombine,
whole check), then escalate. Models are faked at the injected ports; the policy,
split and recombine nodes are the real ones.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from omnimarket.enums.enum_delegation_attempt_kind import EnumDelegationAttemptKind
from omnimarket.models.delegation.model_shrink_retry import (
    EnumShrinkRetryReason,
    EnumShrinkRetryRung,
    EnumShrinkRetryScope,
    ModelShrinkRetryCaps,
    ModelShrinkRetryDecision,
    ModelShrinkRetryDecisionRequest,
)
from omnimarket.models.delegation.model_shrink_retry_outcome import (
    EnumShrinkRetryResolution,
    ModelShrinkUnitOutcome,
    ModelShrinkWholeCheck,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)
from omnimarket.models.delegation_capability_map.enum_capability_cell_verdict import (
    EnumCapabilityCellVerdict,
)
from omnimarket.models.delegation_capability_map.model_capability_cell import (
    ModelCapabilityCell,
)
from omnimarket.models.model_delegation_split_recombine import (
    EnumDelegationSizeBand,
    ModelDelegationSplitUnit,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_shrink_retry import (
    HandlerShrinkRetry,
)
from omnimarket.nodes.node_delegation_shrink_retry_compute.handlers.handler_shrink_retry_policy import (
    HandlerShrinkRetryPolicy,
    next_best_configuration,
)

pytestmark = pytest.mark.unit

UNIT_PROMPT = "One part of a larger request.\n\n{source}"
CAPS = ModelShrinkRetryCaps(
    max_config_swaps=1,
    max_split_depth=2,
    unit_attempts_per_unit=2,
    unit_prompt=UNIT_PROMPT,
)
BACKEND, MODEL = "local-qwen", "qwen-27b"
SECOND = ModelAttemptConfiguration(temperature=0.0, max_tokens=2048)
FIRST_CONFIG = ModelAttemptConfiguration(temperature=0.7, max_tokens=4096)
TENANT = UUID("00000000-0000-0000-0000-000000000001")


def _diff(*files: tuple[str, int]) -> str:
    """A git diff of files, each with the given number of hunks."""
    out: list[str] = []
    for name, hunks in files:
        out.append(f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n")
        for i in range(hunks):
            start = 1 + i * 10
            out.append(
                f"@@ -{start},2 +{start},2 @@\n-old {name} {i}\n+new {name} {i}\n"
            )
    return "".join(out)


class FakePorts:
    """Faked model calls; every unit run records the backend and model it used."""

    def __init__(
        self,
        *,
        failing: frozenset[str] = frozenset(),
        whole_passes: bool = True,
    ) -> None:
        self.failing = failing
        self.whole_passes = whole_passes
        self.unit_runs: list[tuple[str, str, str, str]] = []
        self.escalated: list[str] = []
        self.wholes: list[str] = []

    def _attempt(self, unit: ModelDelegationSplitUnit) -> dict[str, object]:
        return {
            "backend_id": BACKEND,
            "model_id": MODEL,
            "size_band": unit.size_band.value,
            "quality_gate_passed": unit.source not in self.failing,
        }

    async def run_unit(self, unit: ModelDelegationSplitUnit) -> ModelShrinkUnitOutcome:
        self.unit_runs.append((unit.unit_id, BACKEND, MODEL, unit.source))
        ok = not any(marker in unit.source for marker in self.failing)
        return ModelShrinkUnitOutcome(
            answer=f"answer[{unit.unit_id}]", passed=ok, attempt=self._attempt(unit)
        )

    async def escalate_unit(
        self, unit: ModelDelegationSplitUnit
    ) -> ModelShrinkUnitOutcome:
        self.escalated.append(unit.unit_id)
        return ModelShrinkUnitOutcome(
            answer=f"escalated[{unit.unit_id}]",
            passed=True,
            attempt={"backend_id": "next-responder", "model_id": "bigger"},
        )

    async def check_whole(self, answer: str) -> ModelShrinkWholeCheck:
        self.wholes.append(answer)
        return ModelShrinkWholeCheck(
            passed=self.whole_passes,
            attempt={
                "backend_id": BACKEND,
                "model_id": MODEL,
                "quality_gate_passed": self.whole_passes,
            },
        )


async def _settle(
    ports: FakePorts,
    *,
    task_class: str = "code_review",
    source: str,
    band: EnumDelegationSizeBand | None = EnumDelegationSizeBand.LARGE,
    caps: ModelShrinkRetryCaps | None = CAPS,
    swaps_used: int = 0,
    second: ModelAttemptConfiguration | None = None,
    correlation_id: UUID | None = None,
):
    return await HandlerShrinkRetry().settle(
        task_id="t1",
        task_class=task_class,
        size_band=band,
        source=source,
        caps=caps,
        config_swaps_used=swaps_used,
        second_configuration=second,
        ports=ports,
        correlation_id=correlation_id or uuid4(),
        failed_attempt_index=0,
    )


# --- AC1 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shrink_retry_same_model_then_whole_check() -> None:
    ports = FakePorts()
    source = _diff(("a.py", 1), ("b.py", 1), ("c.py", 1))
    cid = uuid4()
    out = await _settle(ports, source=source, correlation_id=cid)

    assert out.resolution is EnumShrinkRetryResolution.ACCEPTED
    # Units ran on the same model and backend, each on only its own slice.
    assert [r[1:3] for r in ports.unit_runs] == [(BACKEND, MODEL)] * 3
    assert [("a.py" in r[3], "b.py" in r[3]) for r in ports.unit_runs] == [
        (True, False),
        (False, True),
        (False, False),
    ]
    # The recombined whole was checked once, and is what was accepted.
    assert len(ports.wholes) == 1
    assert out.answer == ports.wholes[0]
    # The attempt events: split children, then the recombine check, all with lineage.
    kinds = [e["attempt_kind"] for e in out.events]
    assert kinds == ["split_child"] * 3 + ["recombine_check"]
    assert {e["split_id"] for e in out.events} == {out.events[0]["split_id"]}
    parent = str(UUID(str(out.events[0]["parent_attempt_id"])))
    assert all(e["parent_attempt_id"] == parent for e in out.events)
    assert out.unit_attempts == 3
    assert out.split_depth == 1


# --- AC2 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_decomposable_escalates() -> None:
    ports = FakePorts()
    # planning is not decomposable; a one-file one-hunk diff has no boundary either.
    planning = await _settle(ports, task_class="planning", source="plan the thing")
    assert planning.resolution is EnumShrinkRetryResolution.ESCALATE
    assert planning.reason is EnumShrinkRetryReason.NOT_DECOMPOSABLE
    atomic = await _settle(ports, source=_diff(("a.py", 1)))
    assert atomic.resolution is EnumShrinkRetryResolution.ESCALATE
    assert atomic.reason is EnumShrinkRetryReason.NOT_DECOMPOSABLE
    no_band = await _settle(ports, source=_diff(("a.py", 1), ("b.py", 1)), band=None)
    assert no_band.resolution is EnumShrinkRetryResolution.ESCALATE
    assert ports.unit_runs == []
    assert ports.wholes == []


@pytest.mark.asyncio
async def test_whole_fail_escalates() -> None:
    ports = FakePorts(whole_passes=False)
    out = await _settle(ports, source=_diff(("a.py", 1), ("b.py", 1)))
    assert out.resolution is EnumShrinkRetryResolution.ESCALATE
    assert out.reason is EnumShrinkRetryReason.WHOLE_CHECK_FAILED
    assert out.answer is None  # a failed whole is never accepted
    assert [e["attempt_kind"] for e in out.events][-1] == "recombine_check"
    assert len(ports.wholes) == 1


def test_a_class_without_declared_caps_escalates() -> None:
    decision = HandlerShrinkRetryPolicy().handle(
        ModelShrinkRetryDecisionRequest(
            task_id="t",
            task_class="code_review",
            scope=EnumShrinkRetryScope.WHOLE,
            caps=None,
            splittable=True,
            second_configuration=SECOND,
        )
    )
    assert decision.rung is EnumShrinkRetryRung.ESCALATE_WHOLE
    assert decision.reason is EnumShrinkRetryReason.SHRINK_NOT_DECLARED


# --- AC3 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shrink_caps_then_escalate_depth() -> None:
    """Depth 2 is reachable: file unit fails, splits per hunk; a failing hunk escalates."""
    source = _diff(("a.py", 2), ("b.py", 1))
    ports = FakePorts(failing=frozenset({"new a.py 0", "new a.py 1"}))
    out = await _settle(ports, source=source)
    assert out.resolution is EnumShrinkRetryResolution.ACCEPTED
    assert out.split_depth == 2
    kinds = [e["attempt_kind"] for e in out.events]
    assert "unit_escalation" in kinds
    assert kinds[-1] == "recombine_check"
    assert len(ports.escalated) == 2  # hunks 0 and 1 are small and cannot split further

    # With the class cap at depth 1 the failed file unit may not split: it escalates alone.
    capped = ModelShrinkRetryCaps(
        max_config_swaps=1,
        max_split_depth=1,
        unit_attempts_per_unit=2,
        unit_prompt=UNIT_PROMPT,
    )
    ports = FakePorts(failing=frozenset({"new a.py 0"}))
    out = await _settle(ports, source=source, caps=capped)
    assert out.split_depth == 1
    assert ports.escalated == ["t1:0"]


@pytest.mark.asyncio
async def test_shrink_caps_then_escalate_unit_attempts() -> None:
    """Unit attempts stop at twice the unit count; the unit that would pass it escalates."""
    source = _diff(("a.py", 2), ("b.py", 2), ("c.py", 1))
    ports = FakePorts(failing=frozenset({"new a.py 0", "new b.py 0"}))
    out = await _settle(ports, source=source)
    # 3 first attempts, a.py splits into 2 (5 used); b.py would need 2 more (7 > 6).
    assert out.unit_attempts == 5
    assert out.unit_attempts <= CAPS.unit_attempts_per_unit * 3
    # hunk a.py:0 is small and cannot split; b.py would pass the cap
    assert ports.escalated == ["t1:0:0", "t1:1"]
    assert out.resolution is EnumShrinkRetryResolution.ACCEPTED


def test_shrink_caps_then_escalate_policy_reasons() -> None:
    policy = HandlerShrinkRetryPolicy()

    def unit(*, split_depth: int, unit_attempts_used: int) -> ModelShrinkRetryDecision:
        return policy.handle(
            ModelShrinkRetryDecisionRequest(
                task_id="u",
                task_class="code_review",
                scope=EnumShrinkRetryScope.UNIT,
                caps=CAPS,
                splittable=True,
                unit_count=3,
                split_depth=split_depth,
                unit_attempts_used=unit_attempts_used,
            )
        )

    assert (
        unit(split_depth=1, unit_attempts_used=3).rung
        is EnumShrinkRetryRung.RESPLIT_UNIT
    )
    d = unit(split_depth=2, unit_attempts_used=3)
    assert (d.rung, d.reason) == (
        EnumShrinkRetryRung.ESCALATE_UNIT,
        EnumShrinkRetryReason.SPLIT_DEPTH_CAP,
    )
    d = unit(split_depth=1, unit_attempts_used=5)
    assert (d.rung, d.reason) == (
        EnumShrinkRetryRung.ESCALATE_UNIT,
        EnumShrinkRetryReason.UNIT_ATTEMPT_CAP,
    )


# --- AC6 / AC7 -------------------------------------------------------------


def _cell(
    configuration: ModelAttemptConfiguration,
    configuration_id: str,
    *,
    bound: float,
    verdict: EnumCapabilityCellVerdict = EnumCapabilityCellVerdict.MET,
) -> ModelCapabilityCell:
    return ModelCapabilityCell(
        tenant_id=TENANT,
        map_run_id="run-1",
        model_key=MODEL,
        backend_id=BACKEND,
        configuration_id=configuration_id,
        configuration_parameters=configuration,
        task_class="code_review",
        size_band=EnumSizeBand.L,
        n=120,
        passes=118,
        pass_rate=118 / 120,
        wilson_low=0.9,
        wilson_high=0.99,
        decision_lower_bound=bound,
        floor=0.9,
        required_n=89,
        verdict=verdict,
    )


def _second_from_map(
    cells: list[ModelCapabilityCell],
) -> ModelAttemptConfiguration | None:
    return next_best_configuration(
        cells,
        model_key=MODEL,
        backend_id=BACKEND,
        task_class="code_review",
        size_band=EnumSizeBand.L,
        current=FIRST_CONFIG,
    )


@pytest.mark.asyncio
async def test_config_swap_before_shrink() -> None:
    cells = [
        _cell(FIRST_CONFIG, "cfg-1", bound=0.95),
        _cell(SECOND, "cfg-2", bound=0.93),
        _cell(ModelAttemptConfiguration(temperature=1.0), "cfg-3", bound=0.91),
    ]
    second = _second_from_map(cells)
    assert second == SECOND  # the MET cell with the next-highest lower bound
    ports = FakePorts()
    out = await _settle(
        ports, source=_diff(("a.py", 1), ("b.py", 1)), second=second, swaps_used=0
    )
    # The swap rung comes first: no split, no unit attempt, on the same model.
    assert out.resolution is EnumShrinkRetryResolution.CONFIG_SWAP
    assert out.configuration == SECOND
    assert ports.unit_runs == []
    assert ports.wholes == []
    assert out.events == ()
    # When the swapped retry fails again the ladder goes on to shrink.
    again = await _settle(
        ports, source=_diff(("a.py", 1), ("b.py", 1)), second=second, swaps_used=1
    )
    assert again.resolution is EnumShrinkRetryResolution.ACCEPTED
    assert [e["attempt_kind"] for e in again.events][-1] == "recombine_check"


@pytest.mark.asyncio
async def test_config_swap_capped() -> None:
    """A second swap on the same model is refused by the cap, however many cells are MET."""
    ports = FakePorts()
    out = await _settle(
        ports, source=_diff(("a.py", 1), ("b.py", 1)), second=SECOND, swaps_used=1
    )
    assert out.resolution is not EnumShrinkRetryResolution.CONFIG_SWAP
    # With a cap of zero even the first swap is refused.
    zero = ModelShrinkRetryCaps(
        max_config_swaps=0,
        max_split_depth=2,
        unit_attempts_per_unit=2,
        unit_prompt=UNIT_PROMPT,
    )
    out = await _settle(
        ports, source=_diff(("a.py", 1), ("b.py", 1)), second=SECOND, caps=zero
    )
    assert out.resolution is EnumShrinkRetryResolution.ACCEPTED
    # And a non-decomposable class past the cap goes straight to its next responder.
    out = await _settle(
        ports, task_class="planning", source="x", second=SECOND, swaps_used=1
    )
    assert out.resolution is EnumShrinkRetryResolution.ESCALATE


@pytest.mark.asyncio
async def test_config_swap_skipped_without_met_cell() -> None:
    # Only the configuration that just failed is MET; the other cell is MISSED.
    cells = [
        _cell(FIRST_CONFIG, "cfg-1", bound=0.95),
        _cell(SECOND, "cfg-2", bound=0.5, verdict=EnumCapabilityCellVerdict.MISSED),
    ]
    assert _second_from_map(cells) is None
    assert _second_from_map([]) is None
    ports = FakePorts()
    out = await _settle(ports, source=_diff(("a.py", 1), ("b.py", 1)), second=None)
    assert out.resolution is EnumShrinkRetryResolution.ACCEPTED  # went to shrink
    out = await _settle(ports, task_class="planning", source="x", second=None)
    assert out.resolution is EnumShrinkRetryResolution.ESCALATE  # or escalate


def test_attempt_kinds_used_are_the_recorded_kinds() -> None:
    assert {k.value for k in EnumDelegationAttemptKind} >= {
        "split_child",
        "unit_escalation",
        "recombine_check",
        "config_swap",
    }
