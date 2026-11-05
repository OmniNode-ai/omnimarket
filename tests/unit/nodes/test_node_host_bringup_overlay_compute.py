# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host bring-up overlay compute node (OMN-19998 AC11).

Host bring-up was a hand-run script; the declared target graph and the pure plan
already exist as nodes, but nothing turned one host's recorded facts into the
plan's check observations. This node resolves a host overlay (facts keyed by the
symbolic check subject of each declared row) into one observation per target,
and the plan compute folds those into skip-or-act steps.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.host_bringup import EnumObservedState
from omnimarket.nodes.node_host_bringup_overlay_compute.handlers.handler_host_bringup_overlay import (
    HandlerHostBringupOverlay,
    check_rows_from_contract,
    load_host_overlay,
)
from omnimarket.nodes.node_host_bringup_overlay_compute.models import (
    EnumOverlayRefusalKind,
    EnumOverlayStatus,
    ModelHostBringupOverlayRequest,
)
from omnimarket.nodes.node_host_bringup_plan_compute.handlers.handler_host_bringup_plan import (
    HandlerHostBringupPlan,
    load_bringup_graph,
)
from omnimarket.nodes.node_host_bringup_plan_compute.models import (
    EnumPlanStatus,
    EnumStepDecision,
    ModelCheckObservation,
    ModelHostBringupPlanRequest,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO / "tests" / "fixtures" / "host_bringup"
_ORCHESTRATOR = (
    _REPO / "src/omnimarket/nodes/node_host_bringup_orchestrator/contract.yaml"
)


def _yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _request(overlay: dict[str, Any]) -> ModelHostBringupOverlayRequest:
    return ModelHostBringupOverlayRequest(
        checks=check_rows_from_contract(_yaml(_ORCHESTRATOR)),
        overlay=load_host_overlay(overlay),
    )


def test_overlay_resolves_every_declared_target() -> None:
    result = HandlerHostBringupOverlay().handle(
        _request(_yaml(_FIXTURES / "fixture_host_overlay.yaml"))
    )
    assert result.status is EnumOverlayStatus.RESOLVED
    assert result.host == "host_a"
    assert len(result.observations) == 57
    states = {o.target: o.state for o in result.observations}
    for ok in ("card", "ssh-config", "declared", "adopted", "os-installed"):
        assert states[ok] is EnumObservedState.OK
    # two readings that disagree never read as ok
    assert states["host-key"] is EnumObservedState.UNKNOWN
    assert states["key-login"] is EnumObservedState.NOT_OK
    # a target the overlay says nothing about is unknown, never ok
    assert states["root-access"] is EnumObservedState.UNKNOWN
    host_key = next(o for o in result.observations if o.target == "host-key")
    assert host_key.evidence == ("key changed on reinstall", "key confirmed")


def test_overlay_feeds_the_plan_end_to_end() -> None:
    contract = _yaml(_ORCHESTRATOR)
    resolved = HandlerHostBringupOverlay().handle(
        _request(_yaml(_FIXTURES / "fixture_host_overlay.yaml"))
    )
    plan = HandlerHostBringupPlan().handle(
        ModelHostBringupPlanRequest(
            graph=load_bringup_graph(contract),
            observations=tuple(
                ModelCheckObservation(target=o.target, state=o.state)
                for o in resolved.observations
            ),
        )
    )
    assert plan.status is EnumPlanStatus.PLANNED
    decisions = {s.target: s.decision for s in plan.steps}
    assert len(decisions) == 57
    skipped = sorted(t for t, d in decisions.items() if d is EnumStepDecision.SKIP)
    assert skipped == sorted(
        [
            "adopted",
            "card",
            "declared",
            "firmware-settings",
            "ilo-access",
            "os-installed",
            "reservations",
            "ssh-config",
            "volumes",
        ]
    )
    assert decisions["host-key"] is EnumStepDecision.ACT


def test_overlay_refuses_a_subject_no_target_checks() -> None:
    overlay = {
        "host": "host_a",
        "facts": [
            {"subject": "card", "state": "ok", "evidence": "valid"},
            {"subject": "ssh_confg", "state": "ok", "evidence": "typo"},
        ],
    }
    result = HandlerHostBringupOverlay().handle(_request(overlay))
    assert result.status is EnumOverlayStatus.REFUSED
    assert result.observations == ()
    assert [(r.kind, r.names) for r in result.refusals] == [
        (EnumOverlayRefusalKind.UNKNOWN_SUBJECT, ("ssh_confg",))
    ]


def test_overlay_refuses_a_duplicate_target_row() -> None:
    contract = _yaml(_ORCHESTRATOR)
    rows = check_rows_from_contract(contract)
    request = ModelHostBringupOverlayRequest(
        checks=(*rows, rows[0]),
        overlay=load_host_overlay({"host": "host_a", "facts": []}),
    )
    result = HandlerHostBringupOverlay().handle(request)
    assert result.status is EnumOverlayStatus.REFUSED
    assert [(r.kind, r.names) for r in result.refusals] == [
        (EnumOverlayRefusalKind.DUPLICATE_TARGET, (rows[0].target,))
    ]


@pytest.mark.parametrize(
    "fact",
    [
        {"subject": "card", "state": "ok", "evidence": "  "},
        {"subject": "card", "state": "ok"},
        {"subject": "card", "state": "yes", "evidence": "x"},
        {"subject": "/etc/hosts", "state": "ok", "evidence": "x"},
        {"subject": "card", "state": "ok", "evidence": "x", "extra": 1},
    ],
)
def test_overlay_fact_shape_is_refused_at_load(fact: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        load_host_overlay({"host": "host_a", "facts": [fact]})


def test_overlay_output_is_byte_equal_whatever_the_fact_order() -> None:
    overlay = _yaml(_FIXTURES / "fixture_host_overlay.yaml")
    reordered = {**overlay, "facts": list(reversed(overlay["facts"]))}
    first = HandlerHostBringupOverlay().handle(_request(overlay))
    second = HandlerHostBringupOverlay().handle(_request(reordered))
    assert json.dumps(first.model_dump(mode="json")) == json.dumps(
        second.model_dump(mode="json")
    )
    assert [o.target for o in first.observations] == sorted(
        o.target for o in first.observations
    )
