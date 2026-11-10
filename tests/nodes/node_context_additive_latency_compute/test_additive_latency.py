# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 and AC2 of OMN-21134 (TC.12): additive profile latency qualification."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from omnimarket.models.claude_code_binding import ADDITIVE_BUDGET_CAPS_MS
from omnimarket.nodes.node_context_additive_latency_compute.handlers import (
    HandlerAdditiveLatency,
)
from omnimarket.nodes.node_context_additive_latency_compute.models import (
    ModelAdditiveLatencyRequest,
)

pytestmark = pytest.mark.unit

PROFILES = tuple(ADDITIVE_BUDGET_CAPS_MS)
HOSTS = ("launching-mac", "lab-host")


def _trips(
    *,
    profiles: tuple[str, ...] = PROFILES,
    hosts: tuple[str, ...] = HOSTS,
    count: int = 20,
    duration: dict[tuple[str, str, str], float] | None = None,
) -> list[dict[str, object]]:
    """count cold and count warm trips per profile and host.

    The default duration of trip i is i * step where step is a tenth of the
    profile's cap, so the nearest-rank p95 of 20 trips is 19 steps = 1.9 caps / 10.
    """
    rows: list[dict[str, object]] = []
    for profile in profiles:
        step = ADDITIVE_BUDGET_CAPS_MS[profile] / 100
        for host in hosts:
            for temperature in ("cold", "warm"):
                for i in range(1, count + 1):
                    base = step * i
                    if duration is not None:
                        base = duration.get((profile, host, temperature), base)
                    rows.append(
                        {
                            "round_trip_id": f"{profile}-{host}-{temperature}-{i}",
                            "profile": profile,
                            "host": host,
                            "temperature": temperature,
                            "duration_ms": base,
                        }
                    )
    return rows


def _request(
    trips: list[dict[str, object]], **extra: object
) -> ModelAdditiveLatencyRequest:
    body: dict[str, object] = {
        "round_trips": trips,
        "required_profiles": list(PROFILES),
        "required_hosts": list(HOSTS),
    }
    body.update(extra)
    return ModelAdditiveLatencyRequest.model_validate(body)


def test_percentiles_use_nearest_rank_per_profile_host_and_temperature() -> None:
    result = HandlerAdditiveLatency().handle(_request(_trips()))
    cell = next(
        c
        for c in result.cells
        if (c.profile, c.host, c.temperature.value)
        == ("session_start", "lab-host", "warm")
    )
    step = ADDITIVE_BUDGET_CAPS_MS["session_start"] / 100
    assert cell.count == 20
    assert cell.latency.p50_ms == step * 10
    assert cell.latency.p95_ms == step * 19
    assert cell.latency.max_ms == step * 20


def test_one_cell_per_profile_host_and_temperature() -> None:
    result = HandlerAdditiveLatency().handle(_request(_trips()))
    assert result.complete
    assert len(result.cells) == len(PROFILES) * len(HOSTS) * 2


def test_budget_is_the_worst_p95_rounded_up_and_never_above_the_cap() -> None:
    result = HandlerAdditiveLatency().handle(_request(_trips()))
    assert result.falsifiers == ()
    for proposal in result.proposals:
        cap = ADDITIVE_BUDGET_CAPS_MS[proposal.profile]
        worst = cap / 100 * 19
        assert proposal.cap_ms == cap
        assert proposal.worst_p95_ms == worst
        assert proposal.proposed_budget_ms == math.ceil(worst)
        assert proposal.proposed_budget_ms is not None
        assert proposal.proposed_budget_ms >= proposal.worst_p95_ms
        assert proposal.proposed_budget_ms <= cap


def test_a_host_over_the_cap_is_not_promotable_there_and_names_the_warming_remedy() -> (
    None
):
    slow = {("per_prompt", "lab-host", "cold"): 900.0}
    result = HandlerAdditiveLatency().handle(_request(_trips(duration=slow)))
    per_prompt = next(p for p in result.proposals if p.profile == "per_prompt")
    by_host = {v.host: v for v in per_prompt.hosts}
    assert not by_host["lab-host"].promotable
    assert by_host["lab-host"].p95_ms == 900.0
    assert "section 10" in by_host["lab-host"].remedy
    assert by_host["launching-mac"].promotable
    assert by_host["launching-mac"].remedy == ""
    # the budget is taken from the hosts that can carry the row, not from the slow host
    assert per_prompt.proposed_budget_ms == math.ceil(500 / 100 * 19)
    other = next(p for p in result.proposals if p.profile == "session_start")
    assert all(v.promotable for v in other.hosts)


def test_a_profile_over_its_cap_on_every_host_proposes_no_budget() -> None:
    slow = {
        ("agent_spawn", host, temperature): 9000.0
        for host in HOSTS
        for temperature in ("cold", "warm")
    }
    result = HandlerAdditiveLatency().handle(_request(_trips(duration=slow)))
    agent_spawn = next(p for p in result.proposals if p.profile == "agent_spawn")
    assert agent_spawn.proposed_budget_ms is None
    assert not any(v.promotable for v in agent_spawn.hosts)


@pytest.mark.parametrize("missing", PROFILES)
def test_a_missing_profile_is_a_falsifier_and_withholds_every_proposal(
    missing: str,
) -> None:
    kept = tuple(p for p in PROFILES if p != missing)
    result = HandlerAdditiveLatency().handle(_request(_trips(profiles=kept)))
    assert not result.complete
    assert f"profile {missing} missing" in result.falsifiers
    assert all(p.proposed_budget_ms is None for p in result.proposals)
    assert all(not v.promotable for p in result.proposals for v in p.hosts)


def test_a_missing_host_is_a_falsifier_and_withholds_every_proposal() -> None:
    result = HandlerAdditiveLatency().handle(_request(_trips(hosts=("lab-host",))))
    assert not result.complete
    assert "host launching-mac missing" in result.falsifiers
    assert all(p.proposed_budget_ms is None for p in result.proposals)


def test_too_few_warm_round_trips_in_one_cell_is_a_falsifier() -> None:
    trips = [
        t
        for t in _trips()
        if not (
            t["profile"] == "session_start"
            and t["host"] == "lab-host"
            and t["temperature"] == "warm"
            and str(t["round_trip_id"]).endswith(("-19", "-20"))
        )
    ]
    result = HandlerAdditiveLatency().handle(_request(trips))
    assert not result.complete
    assert "cell session_start/lab-host/warm: 18 of 20 round trips" in result.falsifiers
    assert all(p.proposed_budget_ms is None for p in result.proposals)


def test_a_duplicate_round_trip_id_is_refused_so_the_count_cannot_be_padded() -> None:
    trips = _trips()
    trips[1] = {**trips[1], "round_trip_id": trips[0]["round_trip_id"]}
    with pytest.raises(ValidationError, match="duplicate round_trip_id"):
        _request(trips)


def test_a_candidate_budget_below_the_worst_p95_is_a_falsifier() -> None:
    p95 = 5000 / 100 * 19
    result = HandlerAdditiveLatency().handle(
        _request(_trips(), candidate_budgets_ms={"session_start": int(p95) - 1})
    )
    assert any(
        f.startswith("candidate budget session_start") and "below" in f
        for f in result.falsifiers
    )


def test_a_candidate_budget_above_the_cap_is_a_falsifier() -> None:
    result = HandlerAdditiveLatency().handle(
        _request(_trips(), candidate_budgets_ms={"per_prompt": 501})
    )
    assert any(
        f.startswith("candidate budget per_prompt") and "above the cap" in f
        for f in result.falsifiers
    )


def test_a_candidate_budget_inside_the_bounds_passes() -> None:
    result = HandlerAdditiveLatency().handle(
        _request(_trips(), candidate_budgets_ms={"session_start": 4000})
    )
    assert result.falsifiers == ()


def test_a_candidate_cannot_be_checked_against_an_incomplete_table() -> None:
    result = HandlerAdditiveLatency().handle(
        _request(
            _trips(hosts=("lab-host",)), candidate_budgets_ms={"session_start": 4000}
        )
    )
    assert "candidate budget session_start unchecked: table incomplete" in (
        result.falsifiers
    )


def test_a_candidate_for_a_profile_that_is_not_required_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a required profile"):
        _request(_trips(), candidate_budgets_ms={"compaction_orchestrator": 100})


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_negative_and_non_finite_durations_are_refused(bad: float) -> None:
    trips = _trips()
    trips[0] = {**trips[0], "duration_ms": bad}
    with pytest.raises(ValidationError):
        _request(trips)


def test_a_profile_without_a_cap_is_refused() -> None:
    trips = _trips()
    trips[0] = {**trips[0], "profile": "compaction_orchestrator"}
    with pytest.raises(ValidationError, match="additive profile"):
        _request(trips)


def test_fewer_than_twenty_required_samples_cannot_be_configured() -> None:
    with pytest.raises(ValidationError):
        _request(_trips(), min_samples_per_cell=19)


def test_one_required_host_is_refused_because_the_plan_needs_two() -> None:
    with pytest.raises(ValidationError):
        _request(_trips(hosts=("lab-host",)), required_hosts=["lab-host"])


def test_the_result_is_deterministic_for_the_same_input() -> None:
    first = HandlerAdditiveLatency().handle(_request(_trips()))
    second = HandlerAdditiveLatency().handle(_request(list(reversed(_trips()))))
    assert first == second
