# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing ledger facts compute: the row-only ledger facts of one landing tick.

The rows below are the ones the live controller's own derivation was run on beside this node (the same
eighteen rows, the same clock); the expected facts are what both produced.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_pr_landing_ledger_facts_compute import (
    HandlerPrLandingLedgerFacts,
    NodePrLandingLedgerFactsCompute,
    derive_ledger_facts,
)
from omnimarket.nodes.node_pr_landing_ledger_facts_compute.handlers import (
    handler_pr_landing_ledger_facts,
)
from omnimarket.nodes.node_pr_landing_ledger_facts_compute.models.model_landing_ledger_facts import (
    ModelLandingLedgerFacts,
    ModelLandingLedgerRow,
    ModelLandingLedgerRows,
)

NODE_DIR = Path(handler_pr_landing_ledger_facts.__file__).resolve().parents[1]
NOW = datetime(2026, 10, 9, 10, 0, 0, tzinfo=UTC)
KEY = "cause:OmniNode-ai/omnimarket:0123456789ab"

ROWS = [
    (
        "2026-10-09T09:20:00Z",
        "CLAIM",
        "repo-drains-83z",
        "2026-10-09T09:20:00Z | CLAIM | lane=repo-drains-83z | ticket=OMN-1 | parent=orch-1 | drain loop",
    ),
    (
        "2026-10-09T09:30:00Z",
        "CLAIM",
        "repo-drains-84a",
        "2026-10-09T09:30:00Z | CLAIM | lane=repo-drains-84a | ticket=OMN-1 | parent=orch-1 | newer drain",
    ),
    (
        "2026-10-09T09:31:00Z",
        "CLAIM",
        "cause-a",
        f"2026-10-09T09:31:00Z | CLAIM | lane=cause-a | ticket=OMN-2 | cause={KEY} | fix=omnimarket#3600, OmniNode-ai/omnibase_infra#4700,bad,omnimarket#7x | pr=omnimarket#3435",
    ),
    (
        "2026-10-09T09:32:00Z",
        "CLAIM",
        "cause-b",
        "2026-10-09T09:32:00Z | CLAIM | lane=cause-b | ticket=OMN-3 | cause=omnibase_infra:CI Summary | repo=omnibase_infra",
    ),
    (
        "2026-10-09T09:33:00Z",
        "CLAIM",
        "cause-c",
        "2026-10-09T09:33:00Z | CLAIM | lane=cause-c | ticket=OMN-4 | check=Coverage Gate | repo=OmniNode-ai/omnimarket",
    ),
    (
        "2026-10-09T09:34:00Z",
        "CLAIM",
        "landing-L5001",
        f"2026-10-09T09:34:00Z | CLAIM | lane=landing-L5001 | cause={KEY} | relay=landing-controller",
    ),
    (
        "2026-10-09T09:35:00Z",
        "CLAIM",
        "cause-old",
        "2026-10-09T07:00:00Z | CLAIM | lane=cause-old | cause=omnimarket:Old Check",
    ),
    (
        "2026-10-09T09:36:00Z",
        "CLAIM",
        "cause-d",
        "2026-10-09T09:36:00Z | CLAIM | lane=cause-d | cause=omnimarket:Closed Check | fix=omnimarket#1",
    ),
    (
        "2026-10-09T09:37:00Z",
        "TERMINAL",
        "cause-d",
        "2026-10-09T09:37:00Z | TERMINAL | lane=cause-d | closes-CLAIM=2026-10-09T09:36:00Z | outcome=done",
    ),
    (
        "2026-10-09T09:38:00Z",
        "HOLD",
        "ops",
        "2026-10-09T09:38:00Z | HOLD | lane=ops | id=2026-10-09T09:38:00Z-ops | scope=fixer | repo=omnimarket,OmniNode-ai/omnibase_core | until=2026-10-09T12:00:00Z",
    ),
    (
        "2026-10-09T09:39:00Z",
        "HOLD",
        "ops",
        "2026-10-09T09:39:00Z | HOLD | lane=ops | id=2026-10-09T09:39:00Z-ops | scope=fixer | repo=all | until=2026-10-09T09:40:00Z",
    ),
    (
        "2026-10-09T09:41:00Z",
        "HOLD",
        "ops",
        "2026-10-09T09:41:00Z | HOLD | lane=ops | id=2026-10-09T09:41:00Z-ops | scope=fixer | repo=omnidash",
    ),
    (
        "2026-10-09T09:42:00Z",
        "RELEASE",
        "ops",
        "2026-10-09T09:42:00Z | RELEASE | lane=ops | re=2026-10-09T09:41:00Z-ops",
    ),
    (
        "2026-10-09T09:43:00Z",
        "RELEASE",
        "ops",
        f"2026-10-09T09:43:00Z | RELEASE | lane=ops | cause={KEY} | parked cause ends",
    ),
    (
        "2026-10-09T09:44:00Z",
        "RELEASE",
        "ops",
        "2026-10-09T09:44:00Z | RELEASE | lane=ops | result=PASS | LAB PROOF PASS: omnimarket#3600 head 0123456789ab + omnibase_infra#4700 head fedcba9876543210 on lab-202",
    ),
    (
        "2026-10-09T09:45:00Z",
        "RELEASE",
        "ops",
        "2026-10-09T09:45:00Z | RELEASE | lane=ops | result=PASS | LAB PROOF PASS: omnimarket#3600 head 0123456789ab on lab-101",
    ),
    (
        "2026-10-09T09:46:00Z",
        "CLAIM",
        "cause-e",
        "2026-10-09T09:46:00Z | CLAIM | lane=cause-e | cause=omnimarket:Lane Closed",
    ),
    (
        "2026-10-09T09:47:00Z",
        "TERMINAL",
        "cause-e",
        "2026-10-09T09:47:00Z | TERMINAL | lane=cause-e | outcome=done",
    ),
]


def _row(ts: str, rtype: str, lane: str | None, text: str) -> ModelLandingLedgerRow:
    return ModelLandingLedgerRow(ts=ts, rtype=rtype, lane=lane, text=text)


def _request(**kw: object) -> ModelLandingLedgerRows:
    rows = kw.pop("rows", None) or [_row(*r) for r in ROWS]
    return ModelLandingLedgerRows(now=kw.pop("now", NOW), rows=rows, **kw)


def _facts(**kw: object) -> dict[str, Any]:
    return HandlerPrLandingLedgerFacts().handle(_request(**kw)).model_dump(mode="json")


@pytest.mark.unit
def test_contract_declares_a_pure_compute_with_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_pr_landing_ledger_facts_compute"
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["descriptor"]["purity"] == "pure"
    for side, model in (
        ("input_model", ModelLandingLedgerRows),
        ("output_model", ModelLandingLedgerFacts),
    ):
        declared = contract[side]
        module = importlib.import_module(declared["module"])
        assert getattr(module, declared["name"]) is model
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerPrLandingLedgerFacts
    )
    dispatch = contract["runtime_dispatch"]
    assert dispatch["command_topic"].startswith("onex.cmd.omnimarket.")
    assert set(dispatch["terminal_events"]) == {"success", "failure"}


@pytest.mark.unit
def test_handler_is_definition_b() -> None:
    params = list(
        inspect.signature(HandlerPrLandingLedgerFacts.handle).parameters.values()
    )
    assert [p.name for p in params] == ["self", "request"]
    assert not inspect.iscoroutinefunction(HandlerPrLandingLedgerFacts.handle)
    assert issubclass(NodePrLandingLedgerFactsCompute, HandlerPrLandingLedgerFacts)
    assert isinstance(
        HandlerPrLandingLedgerFacts().handle(_request()), ModelLandingLedgerFacts
    )
    assert derive_ledger_facts(_request()) == HandlerPrLandingLedgerFacts().handle(
        _request()
    )


@pytest.mark.unit
def test_handler_does_no_io() -> None:
    tree = ast.parse(Path(handler_pr_landing_ledger_facts.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"os", "subprocess", "socket", "time", "random", "pathlib"}


@pytest.mark.unit
def test_facts_equal_the_live_controllers_on_the_same_rows() -> None:
    assert _facts() == {
        "rows_read": 18,
        "fixer_hold": ["omnibase_core", "omnimarket"],
        "cause_owners": [
            {
                "lane": "cause-a",
                "repo": "OmniNode-ai/omnimarket",
                "check": None,
                "cause": KEY,
            },
            {
                "lane": "cause-b",
                "repo": "OmniNode-ai/omnibase_infra",
                "check": "CI Summary",
                "cause": None,
            },
            {
                "lane": "cause-c",
                "repo": "OmniNode-ai/omnimarket",
                "check": "Coverage Gate",
                "cause": None,
            },
            {
                "lane": "cause-old",
                "repo": "OmniNode-ai/omnimarket",
                "check": "Old Check",
                "cause": None,
            },
        ],
        "cause_releases": [{"cause": KEY, "at": "2026-10-09T09:43:00Z"}],
        "cause_fixes": [
            {"pr": "omnibase_infra#4700", "lane": "cause-a", "cause": KEY},
            {"pr": "omnimarket#3600", "lane": "cause-a", "cause": KEY},
        ],
        "lab_passes": {
            "omnibase_infra#4700": ["fedcba9876543210"],
            "omnimarket#3600": ["0123456789ab"],
        },
        "drain_lane": "repo-drains-84a",
    }


@pytest.mark.unit
def test_a_closed_claim_owns_nothing_and_a_relay_claim_never_owns_a_cause() -> None:
    facts = _facts()
    lanes = {o["lane"] for o in facts["cause_owners"]}
    # cause-d is closed by closes-claim=, cause-e by a later TERMINAL of its lane
    assert not lanes & {"cause-d", "cause-e", "landing-L5001"}
    assert [f["pr"] for f in facts["cause_fixes"]] == [
        "omnibase_infra#4700",
        "omnimarket#3600",
    ]


@pytest.mark.unit
def test_a_lane_silent_for_forty_five_minutes_owns_no_cause_and_no_drain() -> None:
    later = datetime(2026, 10, 9, 10, 31, 0, tzinfo=UTC)
    facts = _facts(now=later)
    # the newest row of any lane is 46 minutes old, so no lane is live
    assert facts["cause_owners"] == []
    assert facts["drain_lane"] == ""


@pytest.mark.unit
def test_fixer_hold_ends_on_a_release_and_on_its_until() -> None:
    held = _facts()["fixer_hold"]
    assert "omnidash" not in held  # lifted by the RELEASE re=
    assert held == ["omnibase_core", "omnimarket"]
    assert _facts(now=datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC))["fixer_hold"] == []


@pytest.mark.unit
def test_a_claim_older_than_the_window_owns_nothing() -> None:
    assert _facts(window_days=0.001)["cause_owners"] == []
    assert len(_facts()["cause_owners"]) == 4


@pytest.mark.unit
def test_history_rows_add_only_lab_pass_readbacks() -> None:
    old = _row(
        "2026-09-01T00:00:00Z",
        "RELEASE",
        "ops",
        "2026-09-01T00:00:00Z | RELEASE | result=PASS | LAB PROOF PASS: omnimarket#3000 head aaaaaaaaaa on lab-202",
    )
    claim = _row(
        "2026-09-01T00:01:00Z",
        "CLAIM",
        "cause-z",
        "2026-09-01T00:01:00Z | CLAIM | lane=cause-z | cause=omnimarket:Z",
    )
    facts = _facts(history_rows=[old, claim])
    assert facts["lab_passes"]["omnimarket#3000"] == ["aaaaaaaaaa"]
    assert facts["cause_owners"] == _facts()["cause_owners"]
    assert facts["rows_read"] == 18


@pytest.mark.unit
def test_a_naive_clock_and_a_malformed_row_stamp_are_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLandingLedgerRows(now=datetime(2026, 10, 9, 10, 0, 0), rows=())
    with pytest.raises(ValidationError):
        _row("2026-10-09", "CLAIM", None, "x")


@pytest.mark.unit
def test_the_same_rows_give_the_same_facts() -> None:
    assert _request().model_dump_json() == _request().model_dump_json()
    assert _facts() == _facts()
