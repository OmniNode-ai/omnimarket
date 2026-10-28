# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21310: a hanging first rung is cut in time for the next rung, and a rung
reached too late is skipped, not recorded as an attempt.

THE DEFECT, found 2026-10-22 on omnimarket dev 29af80ca, receipt run
77e0b51a-8088-4083-afee-9eaba18c2bbf: the first rung's provider call ended
``did not return within 104s``, and every later rung was then recorded as an
attempt with ``rung deadline reached at stage=inference: -19.5s left``. Six of
seven attempts made no provider call; the ladder climbed on paper only.

OMN-21214's rung deadlines were tested on an injected clock where each slow
rung stops at its own deadline. A real call does not: the effect child's
endpoint timeout starts when the child reports ready, so the child's boot
(interpreter start, the ``spawn`` re-import) was spent on top of the cut. Here
the first rung runs through the real process boundary, with a provider stub
that boots slowly and then hangs past any timeout, and no injected clock.

The second half pins the receipt: a rung reached with less than
``rung_floor_seconds`` left is not sent and not listed as an attempt of its
model; the result names it under ``skipped_rungs``. When every rung left is
skipped, the run ends ``failed`` with the typed cause ``timeout``.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)

pytestmark = pytest.mark.unit

_LADDER = ("local", "cheap_cloud", "mid_cloud")
_BACKEND = {"local": "local-coder", "cheap_cloud": "cloud-a", "mid_cloud": "cloud-b"}
_TIER = {value: key for key, value in _BACKEND.items()}
_MODEL = {"local": "slow-model", "cheap_cloud": "model-a", "mid_cloud": "model-b"}
_TIMEOUT_MS = {"local": 300000, "cheap_cloud": 120000, "mid_cloud": 120000}
_GOOD_ANSWER = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the "
    "tradeoff is significant because the evidence shows X; therefore we conclude "
    "Y. See references [12] for the methodical analysis and the risk profile this "
    "approach carries in practice."
)

# The first effect child sleeps this long before it reports ready: a slow
# interpreter boot, made deterministic.
_CHILD_BOOT_DELAY_SECONDS = 2.0
# The dispatch buffer is cut from 10 s so the real-clock tests run in seconds;
# every bound in the port scales with it.
_TEST_DISPATCH_BUFFER_SECONDS = 1.0


def _backend(tier: str) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=_BACKEND[tier],
        model_id=_MODEL[tier],
        endpoint_ref=f"https://{tier}-host.example:8443/v1/chat/completions",
        tier=tier,
        max_tokens=4096,
        timeout_ms=_TIMEOUT_MS[tier],
    )


def _install_ladder(
    monkeypatch: pytest.MonkeyPatch, *, tiers: tuple[str, ...], floor_seconds: int
) -> None:
    def resolve(task_type: str, *, backend_id: str | None = None) -> Any:
        return _backend(tiers[0] if backend_id is None else _TIER[backend_id])

    def next_tier(current: str, excluded: Any, **_: Any) -> str | None:
        later = tiers[tiers.index(current) + 1 :]
        return next((tier for tier in later if tier not in excluded), None)

    monkeypatch.setattr(port_mod, "resolve_delegation_backend", resolve)
    monkeypatch.setattr(port_mod, "next_eligible_tier", next_tier)
    monkeypatch.setattr(port_mod, "first_eligible_tier", lambda *_a, **_k: tiers[0])
    monkeypatch.setattr(
        port_mod, "backend_id_for_tier", lambda t, *_a, **_k: _BACKEND[t]
    )
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda b: _TIER.get(b))
    monkeypatch.setattr(
        port_mod, "sibling_backend_available_in_tier", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        port_mod, "resolve_task_class_max_escalations", lambda _t: len(tiers) - 1
    )
    monkeypatch.setattr(port_mod, "resolve_task_class_tier_order", lambda _t: tiers)
    monkeypatch.setattr(port_mod, "is_free_tier", lambda _tier: False)
    monkeypatch.setattr(port_mod, "tier_max_retries", lambda _tier: 0)
    monkeypatch.setattr(
        port_mod, "resolve_task_class_rung_floor_seconds", lambda _t: floor_seconds
    )
    monkeypatch.setattr(
        port_mod, "_next_rung_reserve_seconds", lambda _t: float(floor_seconds)
    )


def _dispatch(port: LocalDelegationDispatchPort, *, budget: int) -> dict[str, Any]:
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=budget,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )
    return result


@pytest.mark.parametrize("max_escalations", [0, 1])
@pytest.mark.parametrize("failure", ["empty_choices", "empty_body", "timeout"])
def test_no_answer_obeys_the_ladder_attempt_budget(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    max_escalations: int,
) -> None:
    _install_ladder(monkeypatch, tiers=_LADDER, floor_seconds=1)
    monkeypatch.setattr(
        port_mod, "resolve_task_class_max_escalations", lambda _t: max_escalations
    )
    calls: list[str] = []

    def provider(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        calls.append(request.model_tier)
        if len(calls) == 1 and failure != "empty_body":
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=(
                    EnumDelegationFailureClass.INVALID_JSON
                    if failure == "empty_choices"
                    else EnumDelegationFailureClass.TIMEOUT
                ),
                error_message=(
                    "API returned empty choices array"
                    if failure == "empty_choices"
                    else "provider timed out"
                ),
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content="" if len(calls) == 1 else _GOOD_ANSWER,
        )

    port = LocalDelegationDispatchPort(
        effect_handler=provider,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )
    result = _dispatch(port, budget=240)
    assert calls == (["local", "cheap_cloud"] if max_escalations else ["local"])
    assert result["status"] == ("completed" if max_escalations else "failed")
    assert result["escalation_count"] == max_escalations


class _HangingProvider:
    """A provider stub run in the real effect child.

    The ``hang_tier`` rung's call hangs until the dispatch kills the child; a
    ``fail_tier`` rung answers with a provider 503; any other rung answers.
    """

    def __init__(self, hang_tier: str, fail_tier: str | None = None) -> None:
        self.hang_tier = hang_tier
        self.fail_tier = fail_tier

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        if request.model_tier == self.hang_tier:
            time.sleep(3600)
        if request.model_tier == self.fail_tier:
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                error_message="provider answered HTTP 503: upstream overloaded",
                http_status=503,
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD_ANSWER,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _run_through_real_children(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: _HangingProvider,
    tiers: tuple[str, ...],
    budget: int,
    floor: int,
    first_boot_delay: float,
) -> tuple[dict[str, Any], list[tuple[str, float]], list[float], float]:
    """Dispatch through real effect child processes on the real clock.

    The first child sleeps ``first_boot_delay`` before it reports ready, as a
    cold interpreter boot does. Children are forked so the test's own boot
    delay reaches them and the run takes seconds. Returns the result, each
    call's tier and start, each call's end, and when the dispatch started, all
    on ``time.monotonic``.
    """
    _install_ladder(monkeypatch, tiers=tiers, floor_seconds=floor)
    monkeypatch.setattr(
        port_mod, "_DISPATCH_TIMEOUT_BUFFER_SECONDS", _TEST_DISPATCH_BUFFER_SECONDS
    )
    monkeypatch.setattr(
        port_mod,
        "_resolve_effect_process_context",
        lambda: multiprocessing.get_context("fork"),
    )
    boot = {"delay": 0.0}
    real_worker = port_mod._effect_handler_worker

    def slow_boot_worker(handler: Any, request: Any, result_queue: Any) -> None:
        time.sleep(boot["delay"])
        real_worker(handler, request, result_queue)

    monkeypatch.setattr(port_mod, "_effect_handler_worker", slow_boot_worker)

    starts: list[tuple[str, float]] = []
    ends: list[float] = []
    real_child = port_mod._run_effect_handler_with_killable_timeout

    async def recording_child(
        handler: Any, request: ModelLlmDelegationCallRequest, **kwargs: Any
    ) -> ModelLlmDelegationCallResult:
        boot["delay"] = first_boot_delay if not starts else 0.0
        starts.append((request.model_tier, time.monotonic()))
        try:
            return await real_child(handler, request, **kwargs)
        finally:
            ends.append(time.monotonic())

    monkeypatch.setattr(
        port_mod, "_run_effect_handler_with_killable_timeout", recording_child
    )
    port = LocalDelegationDispatchPort(
        effect_handler=provider,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=True,
    )
    started = time.monotonic()
    result = _dispatch(port, budget=budget)
    return result, starts, ends, started


def test_a_hanging_first_rung_is_cut_in_time_for_the_next_rung(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: the real call path, a real hang, a real clock.

    A 14 s chain over two rungs at a 6 s floor: the first rung must be over
    by 8 s. Its child boots for 2 s and then hangs.
    """
    budget, floor = 14, 6
    result, starts, ends, started = _run_through_real_children(
        tmp_path,
        monkeypatch,
        provider=_HangingProvider(hang_tier="local"),
        tiers=("local", "cheap_cloud"),
        budget=budget,
        floor=floor,
        first_boot_delay=_CHILD_BOOT_DELAY_SECONDS,
    )

    # The first rung's call, its child's boot included, is over before the
    # chain has only the second rung's floor left.
    first_rung_ended = ends[0] - started
    assert first_rung_ended <= budget - floor, (
        f"the first rung's call ended {first_rung_ended:.1f}s into the "
        f"{budget}s chain, leaving the second rung under its {floor}s floor"
    )
    assert [tier for tier, _ in starts] == ["local", "cheap_cloud"], starts
    assert "skipped_rungs" not in result, result["skipped_rungs"]
    assert result["status"] == "completed", result
    assert [a["tier"] for a in result["attempts"]] == ["local", "cheap_cloud"]
    assert result["attempts"][0]["failure_class"] == (
        EnumDelegationFailureClass.TIMEOUT.value
    )
    assert ends[-1] - started <= budget, f"the run took {ends[-1] - started:.1f}s"


def test_the_second_rung_makes_a_real_provider_call_after_a_slow_first_rung(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3, the reported shape: a slow-booting, hanging first rung, then two more.

    An 18 s chain over three rungs at a 6 s floor. The first child boots for
    5 s, then hangs. The second rung's provider answers HTTP 503 and the third
    answers. Attempt 2 must carry the provider's status, not a rung-deadline
    refusal.
    """
    result, starts, _, _ = _run_through_real_children(
        tmp_path,
        monkeypatch,
        provider=_HangingProvider(hang_tier="local", fail_tier="cheap_cloud"),
        tiers=_LADDER,
        budget=18,
        floor=6,
        first_boot_delay=5.0,
    )

    assert [tier for tier, _ in starts] == list(_LADDER), starts
    _assert_no_deadline_refusal_attempt(result)
    second = result["attempts"][1]
    assert second["model_id"] == _MODEL["cheap_cloud"], result["attempts"]
    assert second["http_status"] == 503, second
    assert result["status"] == "completed", result


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _OverrunningFirstRung:
    """The first rung's call returns ``overrun_to`` seconds into the chain.

    It stands for a call that outlasted the cut it was given; every later rung
    answers at once.
    """

    def __init__(self, clock: _Clock, overrun_to: float) -> None:
        self.clock = clock
        self.started = clock.now
        self.overrun_to = overrun_to
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.model_tier)
        if request.model_tier == "local":
            self.clock.now = self.started + self.overrun_to
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=EnumDelegationFailureClass.TIMEOUT,
                error_message="killed at the dispatch deadline",
            )
        self.clock.now += 2.0
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD_ANSWER,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _run_with_overrun(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, *, overrun_to: float
) -> tuple[dict[str, Any], _OverrunningFirstRung]:
    _install_ladder(monkeypatch, tiers=_LADDER, floor_seconds=45)
    clock = _Clock()
    effect = _OverrunningFirstRung(clock, overrun_to)
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
        clock=clock,
    )
    return _dispatch(port, budget=240), effect


def _assert_no_deadline_refusal_attempt(result: dict[str, Any]) -> None:
    for attempt in result["attempts"]:
        message = str(attempt.get("error_message") or "")
        assert "rung deadline reached" not in message, attempt
        assert "s left" not in message, attempt


def test_skipped_rung_is_named_not_recorded_as_an_attempt(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: rung 2 is reached with 5 s left of its 45 s floor; rung 3 answers.

    The chain is 240 s over three rungs at a 45 s floor, so rung 2 must end
    by 195 s. The first rung's call returns at 190 s.
    """
    result, effect = _run_with_overrun(tmp_path, monkeypatch, overrun_to=190.0)

    assert effect.calls == ["local", "mid_cloud"], effect.calls
    assert result["status"] == "completed", result
    _assert_no_deadline_refusal_attempt(result)
    assert [a["model_id"] for a in result["attempts"]] == [
        _MODEL["local"],
        _MODEL["mid_cloud"],
    ], result["attempts"]
    skipped = result["skipped_rungs"]
    assert [(s["tier"], s["backend_id"], s["model_id"]) for s in skipped] == [
        ("cheap_cloud", _BACKEND["cheap_cloud"], _MODEL["cheap_cloud"])
    ]
    assert skipped[0]["rung_floor_seconds"] == 45
    assert 0 <= skipped[0]["seconds_left"] < 45


def test_skipped_rungs_to_the_end_fail_with_a_typed_timeout(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 and C16: no rung left with its floor; the run names a typed cause."""
    result, effect = _run_with_overrun(tmp_path, monkeypatch, overrun_to=200.0)

    assert effect.calls == ["local"], effect.calls
    assert result["status"] == "failed", result
    assert result["terminal_failure_cause"] == "timeout", result
    _assert_no_deadline_refusal_attempt(result)
    assert [a["tier"] for a in result["attempts"]] == ["local"]
    assert [s["tier"] for s in result["skipped_rungs"]] == [
        "cheap_cloud",
        "mid_cloud",
    ]
    assert "skipped" in result["error_message"], result["error_message"]
