# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch-venv drift is a typed VENV_DRIFT placement result, never a run of no-host (OMN-20862).

On 2026-10-10 the launching host's dispatch venv drifted from the lab's and every lane
went no-host until a hand reconcile. The placement node now takes the dispatch-venv lock
hash of the launching host and of each reading. When the hashes differ and that drift is
the only thing keeping a host from taking the lane, the answer is one VENV_DRIFT result
naming both hashes and one reconcile intent; equal hashes admit as before; a missing hash
on a drift-judged path is VENV_UNKNOWN, never a pass.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    ModelRemoteLanePlacementRequest,
    ModelRemoteLanePlacementResult,
)

LAUNCHING = "a1" * 32
LAB = "b2" * 32
OTHER = "c3" * 32


def _reading(name: str, venv: str | None, **over: Any) -> dict[str, Any]:
    reading: dict[str, Any] = {
        "name": name,
        "engines": ["claude_sonnet", "codex"],
        "lane_slots": 2,
        "lane_cap": 4,
        "rank_free": 5.0,
        "mem_avail_gb": 32.0,
    }
    if venv is not None:
        reading["dispatch_venv_hash"] = venv
    reading.update(over)
    return reading


def _mac(venv: str | None = LAUNCHING, **over: Any) -> dict[str, Any]:
    """The launching host, over its load bar so it takes no lane (the 2026-10-10 shape)."""
    over.setdefault("lane_admission_refusal", "load: 9.1 per core")
    over.setdefault("lane_slots", 0)
    return _reading("mac", venv, local=True, **over)


def _place(
    readings: list[dict[str, Any]], *, launching: str | None = LAUNCHING, **over: Any
) -> ModelRemoteLanePlacementResult:
    payload: dict[str, Any] = {
        "engine": "claude_sonnet",
        "readings": readings,
        **over,
    }
    if launching is not None:
        payload["launching_venv_hash"] = launching
    return HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest.model_validate(payload)
    )


@pytest.mark.unit
def test_drift_returns_venv_drift_naming_both_hashes_and_one_reconcile() -> None:
    result = _place(
        [_reading("h201", LAB), _reading("h202", OTHER), _mac()],
    )
    assert result.outcome == "VENV_DRIFT"
    assert result.host is None
    assert result.venv_drift is not None
    assert result.venv_drift.launching_hash == LAUNCHING
    assert {(h.host, h.hash) for h in result.venv_drift.hosts} == {
        ("h201", LAB),
        ("h202", OTHER),
    }
    # exactly one reconcile intent, covering every drifted host, not one per lane or host
    assert result.reconcile is not None
    assert not isinstance(result.reconcile, tuple | list)
    assert set(result.reconcile.hosts) == {"h201", "h202"}
    assert result.reconcile.target_hash == LAUNCHING
    verdicts = {v.host: v.reason for v in result.verdicts}
    assert verdicts["h201"] == "venv-drift"
    assert verdicts["h202"] == "venv-drift"


@pytest.mark.unit
def test_equal_hashes_admit() -> None:
    result = _place([_reading("h201", LAUNCHING), _mac()])
    assert result.outcome == "ADMITTED"
    assert result.host == "h201"
    assert result.venv_drift is None
    assert result.reconcile is None


@pytest.mark.unit
@pytest.mark.parametrize("engine", ["claude_sonnet", "codex"])
@pytest.mark.parametrize("pinned", [None, "h201"])
def test_every_host_refused_for_drift_is_venv_drift_never_a_bare_no_host(
    engine: str, pinned: str | None
) -> None:
    result = _place(
        [_reading("h201", LAB), _reading("h202", LAB), _mac()],
        engine=engine,
        pinned_host=pinned,
    )
    # the lane found no host; the test fails unless the reason is the VENV_DRIFT result
    assert result.host is None
    assert result.outcome != "NO_HOST"
    assert result.outcome == "VENV_DRIFT"
    payload = result.model_dump(mode="json")
    assert payload["outcome"] == "VENV_DRIFT"
    assert payload["venv_drift"]["launching_hash"] == LAUNCHING
    assert payload["reconcile"] is not None


@pytest.mark.unit
def test_a_pinned_host_that_matches_admits_though_others_drifted() -> None:
    result = _place(
        [_reading("h201", LAUNCHING), _reading("h202", LAB), _mac()],
        pinned_host="h201",
    )
    assert result.outcome == "ADMITTED"
    assert result.host == "h201"


@pytest.mark.unit
def test_a_drifted_host_is_skipped_for_an_equal_one() -> None:
    result = _place(
        [_reading("h201", LAB), _reading("h202", LAUNCHING), _mac()],
    )
    assert result.outcome == "ADMITTED"
    assert result.host == "h202"
    assert result.reconcile is None
    verdicts = {v.host: v.reason for v in result.verdicts}
    assert verdicts["h201"] == "venv-drift"


@pytest.mark.unit
def test_the_launching_host_takes_the_lane_when_every_lab_host_drifted() -> None:
    result = _place(
        [
            _reading("h201", LAB),
            _mac(lane_admission_refusal=None, lane_slots=2),
        ],
    )
    assert result.outcome == "ADMITTED"
    assert result.host == "mac"
    assert result.local is True


@pytest.mark.unit
def test_drift_that_is_not_why_no_host_is_still_no_host() -> None:
    # h201 drifted but also has no lane slot: a reconcile would not give it one.
    result = _place(
        [_reading("h201", LAB, lane_slots=0), _mac()],
    )
    assert result.outcome == "NO_HOST"
    assert result.venv_drift is None
    assert result.reconcile is None


@pytest.mark.unit
def test_a_usage_limited_host_is_not_blamed_on_drift() -> None:
    result = _place(
        [_reading("h201", LAB), _mac()],
        limited_hosts=["h201"],
    )
    assert result.outcome == "NO_HOST"
    assert result.reconcile is None


@pytest.mark.unit
def test_a_missing_host_hash_is_venv_unknown_never_a_pass() -> None:
    result = _place(
        [_reading("h201", None), _mac()],
    )
    assert result.outcome == "VENV_UNKNOWN"
    assert result.host is None
    assert result.reconcile is None
    assert {v.host: v.reason for v in result.verdicts}["h201"] == "venv-unknown"


@pytest.mark.unit
def test_a_missing_launching_hash_is_venv_unknown_when_hosts_report_one() -> None:
    result = _place([_reading("h201", LAB), _mac(LAB)], launching=None)
    assert result.outcome == "VENV_UNKNOWN"
    assert result.host is None


@pytest.mark.unit
def test_a_drift_required_request_with_no_hashes_at_all_is_venv_unknown() -> None:
    result = _place(
        [_reading("h201", None), _mac(None)],
        launching=None,
        dispatch_venv_required=True,
    )
    assert result.outcome == "VENV_UNKNOWN"
    assert result.host is None


@pytest.mark.unit
def test_an_old_producer_sending_no_hashes_places_as_before() -> None:
    admitted = _place([_reading("h201", None), _mac(None)], launching=None)
    assert admitted.outcome == "ADMITTED"
    assert admitted.host == "h201"
    refused = _place([_mac(None)], launching=None)
    assert refused.outcome == "NO_HOST"
    assert refused.host is None
    assert refused.venv_drift is None


@pytest.mark.unit
def test_an_empty_hash_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError):
        ModelRemoteLanePlacementRequest.model_validate(
            {"engine": "codex", "launching_venv_hash": "", "readings": []}
        )
