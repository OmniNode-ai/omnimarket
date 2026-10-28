# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill behaves the same under the `local` and the `lab` profile of factory_bindings (OMN-21475, T-S3, AC6).

The dependencies the fill declares are BKL, LED, CHG, HST, SRC, BUS and CLK. Only BUS and CLK have a
protocol and stubs today, so the other five are supplied as the facts a binding would read: the backlog
items (BKL), the keys already sent (LED), the hosts (HST), the receipts (SRC) and the time (CLK). Each
profile's binding yields those facts; the fill runs on them and the semantic projection of the two
runs is compared by the parity run's own comparison. A binding that reorders rows, drops a row or moves
a source time across the fill's window boundary makes the comparison fail.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_binding_conformance_compute.handlers.handler_binding_conformance import (
    HandlerBindingConformance,
)
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_parity import (
    ModelScenarioRow,
    ModelScenarioRun,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    ModelFactoryBindingsOverlay,
)
from omnimarket.nodes.node_idle_slot_fill_effect import models
from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as handler,
)
from tests.nodes.node_binding_conformance_compute.test_check_bindings import (
    DATA,
    _real_request,
)

pytestmark = pytest.mark.unit

NOW = "2026-10-18T21:30:00Z"
CODES = ("BKL", "LED", "CHG", "HST", "SRC", "BUS", "CLK")
BACKLOG = [
    {"kind": "ticket", "key": "t-1", "repo": "omnimarket", "ticket": "OMN-1"},
    {
        "kind": "ticket",
        "key": "t-2",
        "repo": "omnimarket",
        "ticket": "OMN-2",
        "required_engine": "claude",
    },
    {"kind": "ticket", "key": "t-3", "repo": "omnimarket", "ticket": "OMN-3"},
    {
        "kind": "ticket",
        "key": "prose",
        "repo": "omnimarket",
        "ticket": "OMN-4",
        "required_engine": "the best one",
    },
]
HOSTS = [
    {
        "name": "host-a",
        "local": False,
        "admission": {"claude_slots": 2, "codex_slots": 0, "probe_error": None},
    }
]
SENT_INSIDE_WINDOW = {"t-1": {"lane": "fill-old", "at": "2026-10-18T18:30:01Z"}}

Facts = dict[str, Any]


def reference() -> Facts:
    return {
        "backlog": [dict(i) for i in BACKLOG],
        "dispatched": {k: dict(v) for k, v in SENT_INSIDE_WINDOW.items()},
        "hosts": HOSTS,
        "now": NOW,
    }


def reorders_rows() -> Facts:
    facts = reference()
    facts["backlog"] = [facts["backlog"][2], facts["backlog"][1], facts["backlog"][0]]
    facts["backlog"].append(BACKLOG[3])
    return facts


def drops_a_row() -> Facts:
    facts = reference()
    facts["backlog"] = [i for i in facts["backlog"] if i["key"] != "t-2"]
    return facts


def moves_a_time_across_the_window() -> Facts:
    facts = reference()
    facts["dispatched"]["t-1"]["at"] = (
        "2026-10-18T18:29:59Z"  # 3h00m01s, past the window
    )
    return facts


BINDINGS: dict[str, Callable[[], Facts]] = {
    "reference": reference,
    "reorders_rows": reorders_rows,
    "drops_a_row": drops_a_row,
    "moves_a_time": moves_a_time_across_the_window,
}


def overlay(lab_binding: str) -> ModelFactoryBindingsOverlay:
    return ModelFactoryBindingsOverlay.model_validate(
        {
            "profiles": [{"id": "local", "baseline": True}, {"id": "lab"}],
            "bindings": {
                "local": dict.fromkeys(CODES, "reference"),
                "lab": dict.fromkeys(CODES, lab_binding),
            },
            "proof_bindings": ["reference"],
        }
    )


def run_fill(profile_id: str, binding_id: str) -> ModelScenarioRun:
    facts = BINDINGS[binding_id]()
    result = handler.HandlerIdleSlotFill().handle(
        models.ModelIdleSlotFillRequest(
            now=facts["now"],
            hosts=facts["hosts"],
            lines=["MISSING lab-headroom:host-a | free Claude slots 3 | FIX: dispatch"],
            backlog=facts["backlog"],
            dispatched=facts["dispatched"],
            standing_tickets={"ticket": "OMN-9101"},
        )
    )
    return ModelScenarioRun(
        profile_id=profile_id,
        binding=profile_id,
        proof_eligible=True,
        verdicts=("PASS",),
        lines=tuple(f"skipped {s['key']}: {s['why']}" for s in result.skipped),
        rows=tuple(
            ModelScenarioRow(
                key=s["key"],
                content=json.dumps(
                    {k: s[k] for k in ("kind", "lane", "model", "repo", "ticket")},
                    sort_keys=True,
                ),
            )
            for s in result.sends
        ),
        counts={
            "sends": len(result.sends),
            "skipped": len(result.skipped),
            "backlog_mismatched": result.backlog_mismatched,
        },
    )


def parity(lab_binding: str) -> Any:
    chosen = overlay(lab_binding)
    runs = tuple(
        run_fill(profile.id, chosen.binding_id(profile.id, "BKL"))
        for profile in chosen.profiles
    )
    return compare_runs(runs)


def test_the_fill_projects_the_same_under_the_local_and_the_lab_profile() -> None:
    result = parity("reference")
    assert result.passed, result.first_difference
    baseline = run_fill("local", "reference")
    assert [r.key for r in baseline.rows] == ["t-2", "t-3"]
    assert baseline.counts["backlog_mismatched"] == 1


@pytest.mark.parametrize(
    ("binding", "difference"),
    [
        ("reorders_rows", "row order"),
        ("drops_a_row", "missing row 't-2'"),
        ("moves_a_time", "line at index"),
    ],
)
def test_a_stub_that_reorders_drops_or_moves_a_time_fails_the_comparison(
    binding: str, difference: str
) -> None:
    result = parity(binding)
    assert not result.passed
    assert difference in result.first_difference


def test_the_fill_declares_exactly_the_codes_of_its_row_of_the_protocol_table() -> None:
    contract = yaml.safe_load(
        (Path(handler.__file__).resolve().parents[1] / "contract.yaml").read_text(
            encoding="utf-8"
        )
    )
    declared = [d["name"] for d in contract["protocol_dependencies"]]
    [row] = [r for r in DATA["rows"] if r["node"] == "node_idle_slot_fill_effect"]
    assert sorted(declared) == sorted(row["protocols"]) == sorted(CODES)
    assert all(d["required"] for d in contract["protocol_dependencies"])


def test_the_conformance_check_finds_no_fault_in_the_fills_declarations() -> None:
    """The contract rules pass; the fill's own I/O (env, paths, subprocesses) is not yet in the checked set."""
    result = HandlerBindingConformance().handle(
        _real_request(["node_idle_slot_fill_effect"])
    )
    contract_rules = {
        "contract_missing_protocol",
        "contract_extra_protocol",
        "contract_unknown_protocol",
        "contract_unreadable",
        "no_table_row",
        "no_contract",
    }
    assert [i for i in result.issues if i.rule.value in contract_rules] == []
