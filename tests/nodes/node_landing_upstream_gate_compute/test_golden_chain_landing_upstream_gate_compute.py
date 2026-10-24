# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Direct upstream gate assertions extracted from the controller tests (OMN-19998)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.nodes.node_landing_upstream_gate_compute import (
    HandlerLandingUpstreamGate,
)
from omnimarket.nodes.node_landing_upstream_gate_compute.handlers.handler_landing_upstream_gate import (
    upstream_gate,
)
from omnimarket.nodes.node_landing_upstream_gate_compute.models import (
    ModelLandingUpstreamGateRequest as GateRequest,
)

PR, REF = "project-a#1158", "project-a#1155"
HEAD = "db107aeaee8635425fe7966f7b607319a4e37bbb"
JUDGED_AT = "2026-10-22T05:40:18Z"
JUDGED = {PR: {REF: {"at": JUDGED_AT, "head": HEAD, "kind": "upstream_open"}}}
START = datetime(2026, 10, 22, 5, 40, 18, tzinfo=UTC)


def _stamp(minutes: int) -> str:
    return (START + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _records(state: str) -> dict[str, Any]:
    return {REF: {"facts": {"state": state}}}


def _request(
    judged: Any,
    *,
    records: Any = None,
    now: str = _stamp(0),
    pr: str = PR,
    head: str = HEAD,
) -> GateRequest:
    return GateRequest(
        pr=pr,
        head=head,
        judged=judged,
        records=_records("OPEN") if records is None else records,
        now=now,
    )


def test_an_open_upstream_ref_has_no_backstop() -> None:
    got = upstream_gate(_request(JUDGED[PR], now=_stamp(60 * 24 * 7)))
    assert (got.standing, got.unbounded, got.gate_ref) == ((REF,), (REF,), REF)


@pytest.mark.parametrize(
    "kind", ["operator_decision", "production_gate", "collaborator", "hold_row"]
)
def test_every_other_known_kind_keeps_the_hour(kind: str) -> None:
    judged = {REF: {**JUDGED[PR][REF], "kind": kind}}
    inside = upstream_gate(_request(judged, now=_stamp(59)))
    past = upstream_gate(_request(judged, now=_stamp(60)))
    assert (inside.gate_ref, inside.unbounded) == (REF, ())
    assert (past.gate_ref, past.unbounded, past.standing) == ("", (), (REF,))


@pytest.mark.parametrize("state", ["MERGED", "CLOSED", "UNKNOWN"])
def test_a_ref_that_is_not_open_holds_nothing(state: str) -> None:
    got = upstream_gate(_request(JUDGED[PR], records=_records(state)))
    assert (got.standing, got.unbounded, got.gate_ref) == ((), (), "")


def test_the_blocker_stands_only_at_the_head_it_was_judged_at() -> None:
    assert upstream_gate(_request(JUDGED[PR], head="b" * 40)).standing == ()


def test_a_ref_that_is_the_pr_or_no_pr_or_an_unknown_kind_never_stands() -> None:
    one = JUDGED[PR][REF]
    assert (
        upstream_gate(
            _request({PR: one}, records={PR: {"facts": {"state": "OPEN"}}})
        ).standing
        == ()
    )
    assert upstream_gate(_request({"2026-10-22T05:40:18Z": one})).standing == ()
    assert upstream_gate(_request({REF: {**one, "kind": "waiting_ci"}})).standing == ()


def test_an_unreadable_judgement_time_holds_a_bounded_kind_never() -> None:
    judged = {REF: {"head": HEAD, "kind": "hold_row"}}
    got = upstream_gate(_request(judged))
    assert (got.standing, got.gate_ref) == ((REF,), "")


@pytest.mark.parametrize("minutes", [1, 59, 60, 61, 120, 600, 60 * 24])
def test_recorded_hold_keeps_the_gate_past_the_hour_and_day(minutes: int) -> None:
    result = HandlerLandingUpstreamGate().handle(
        _request(JUDGED[PR], now=_stamp(minutes))
    )
    assert result.gate_ref == REF


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_recorded_hold_lifts_when_upstream_ends(state: str) -> None:
    result = HandlerLandingUpstreamGate().handle(
        _request(JUDGED[PR], now=_stamp(61), records=_records(state))
    )
    assert result.gate_ref == ""


def test_recorded_hold_lifts_when_head_moves() -> None:
    result = HandlerLandingUpstreamGate().handle(
        _request(JUDGED[PR], now=_stamp(61), head="c" * 40)
    )
    assert result.gate_ref == ""


def test_contract_default_handler_resolves_to_the_packaged_handler() -> None:
    import importlib
    from pathlib import Path

    import yaml

    import omnimarket.nodes.node_landing_upstream_gate_compute as package

    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    assert contract["name"] == "node_landing_upstream_gate_compute"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["metadata"]["transport_type"] == "local"
    assert contract["descriptor"]["runtime_profiles"] == []
    assert contract["runtime_dispatch"]["external_trigger"].strip()
    assert "terminal_event" not in contract
    module_name, class_name = contract["handler_routing"]["default_handler"].split(":")
    handler_class = getattr(importlib.import_module(module_name), class_name)
    assert callable(getattr(handler_class(), "handle", None))
