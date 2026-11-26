# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The 2026-11-26 failure catalogue, replayed through the classifier and the reducer (OMN-26701).

Each fixture under ``tests/fixtures/lane_work_catalogue/2026-11-26/`` is a pinned
snapshot of one failure from that day: the raw results in order, quoted verbatim
from the ledger rows, remote-lane receipts and open-work rows the lane work
orchestrator plan (section 4) names, each with its source and the sha256 of the
quoted text, plus the state and action the machine must reach.

STATE OF THIS FILE: RED BY DESIGN. The replay tests import the classifier
(``node_lane_work_event_classify_compute``, task 4) and the reducer
(``node_lane_work_reducer``, task 3), neither of which exists yet. Until tasks 3
and 4 land, every replay test fails with ``RED: ...`` naming the missing module.
It is not skipped: a skip would let the first slice pass without ever being
judged (the acceptance for task 5 says ``pytest -rs`` reports no skipped case).

The fixture checks (``test_ac1_*``) need no node and pass today.
"""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "lane_work_catalogue"
    / "2026-11-26"
)

CASES = (
    "case_01_push_guard",
    "case_01b_missing_remote",
    "case_02_release_identity",
    "case_06_never_started",
    "case_07_head_moved",
    "case_09_base_inherited",
    "case_10_worker_lost",
    "case_12_cause_unknown",
)

# The W rows each case must quote (plan section 4).
W_ROWS = {
    "case_01_push_guard": {"W943", "W951", "W953"},
    "case_01b_missing_remote": {"W900", "W902"},
    "case_02_release_identity": {"W929", "W940", "W947", "W949", "W953"},
    "case_06_never_started": {"W937", "W939", "W959"},
    "case_07_head_moved": {"W916", "W919", "W955"},
    "case_09_base_inherited": {"W942", "W949"},
    "case_10_worker_lost": {"W913", "W936"},
    "case_12_cause_unknown": {"W950"},
}

ROOTS = {"ledger", "remote-lane-receipts", "open-work"}
CLASSIFIER_MODULE = (
    "omnimarket.nodes.node_lane_work_event_classify_compute.handlers."
    "handler_lane_work_event_classify"
)
REDUCER_MODULE = (
    "omnimarket.nodes.node_lane_work_reducer.handlers.handler_lane_work_reducer"
)


def load(case: str) -> dict[str, Any]:
    loaded = yaml.safe_load((FIXTURES / f"{case}.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


# --------------------------------------------------------------------------- AC1


def test_ac1_eight_fixture_files() -> None:
    names = sorted(p.stem for p in FIXTURES.glob("*.yaml"))
    assert names == sorted(CASES), (
        f"expected exactly {len(CASES)} fixtures, got {names}"
    )


@pytest.mark.parametrize("case", CASES)
def test_ac1_every_source_is_complete(case: str) -> None:
    fx = load(case)
    sources = fx["sources"]
    assert sources, f"{case}: sources is empty"
    ids = [s["id"] for s in sources]
    assert len(ids) == len(set(ids)), f"{case}: duplicate source id"
    for s in sources:
        where = f"{case}:{s['id']}"
        assert s["root"] in ROOTS, f"{where}: root {s['root']!r}"
        assert s["path"], f"{where}: no path under the root"
        assert s["quote"], f"{where}: empty quote"
        assert hashlib.sha256(s["quote"].encode()).hexdigest() == s["sha256"], (
            f"{where}: sha256 does not match the quote"
        )
        if s["root"] == "ledger":
            assert s["row_timestamp"], f"{where}: ledger row needs its timestamp"
            assert s["lane"], f"{where}: ledger row needs its lane"
        if s["root"] == "remote-lane-receipts":
            assert s["lane"], f"{where}: receipt needs its lane directory"
        if s["root"] == "open-work":
            assert s["w_row"].startswith("W"), (
                f"{where}: open-work source needs its W row"
            )
        assert "redacted" in s, f"{where}: says whether the quote was redacted"


@pytest.mark.parametrize("case", CASES)
def test_ac1_quotes_the_w_rows_the_plan_names(case: str) -> None:
    fx = load(case)
    quoted = {s["w_row"] for s in fx["sources"] if s["root"] == "open-work"}
    assert quoted == W_ROWS[case]
    assert any(s["root"] == "ledger" for s in fx["sources"]), (
        f"{case}: no ledger row quoted"
    )


@pytest.mark.parametrize("case", CASES)
def test_ac1_replay_steps_point_at_real_sources(case: str) -> None:
    fx = load(case)
    ids = {s["id"] for s in fx["sources"]}
    assert fx["replay"], f"{case}: nothing to replay"
    for step in fx["replay"]:
        if step["step"] == "raw_result":
            assert step["source"] in ids
        if step["step"] == "typed_result":
            assert step["derived_from"] in ids
        assert "expect" in step
    assert fx["replay"][-1]["expect"]["state"] == fx["expected"]["final_state"]


# ----------------------------------------------------------------------- replay


def _need(module: str, task: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # red until the task lands, never a skip
        pytest.fail(f"RED: {task} has not landed; cannot import {module} ({exc})")


@pytest.mark.parametrize("case", CASES)
def test_catalogue_replay(case: str) -> None:
    """Raw results go through the classifier, events through the reducer, ticks
    through the fixture's fixed clock; the machine ends in the expected state
    with the expected action.

    The call shapes below are the contract tasks 3 and 4 must meet:

    * ``HandlerLaneWorkEventClassify().classify(raw_result=<quote>, state=<state>)``
      returns an event with ``kind`` and ``cls`` (or ``red_kind``), or ``None``
      for a result that is an acknowledgement.
    * ``HandlerLaneWorkReducer().fold(state, event, now=<fixed clock>)`` returns
      ``(new_state, intents)``; ``intents[0].kind`` names the action.
    """
    classifier_mod = _need(CLASSIFIER_MODULE, "task 4 (the event classifier)")
    reducer_mod = _need(REDUCER_MODULE, "task 3 (the lane work reducer)")
    classifier = classifier_mod.HandlerLaneWorkEventClassify()
    reducer = reducer_mod.HandlerLaneWorkReducer()

    fx = load(case)
    by_id = {s["id"]: s for s in fx["sources"]}
    state = reducer.initial_state(fx["entry_state"], fx)

    for n, step in enumerate(fx["replay"], start=1):
        where = f"{case} step {n} ({step['step']})"
        want = step["expect"]
        if step["step"] == "raw_result":
            event = classifier.classify(
                raw_result=by_id[step["source"]]["quote"], state=state
            )
        elif step["step"] == "typed_result":
            event = classifier.classify_typed(
                step["kind"], step["payload"], state=state
            )
        else:
            event = reducer_mod.deadline_event(
                step["deadline"], step["at"], state=state
            )
        assert event is not None, f"{where}: no event emitted"
        for key, value in want["event"].items():
            assert getattr(event, key) == value, f"{where}: event {key}"
        state, intents = reducer.fold(state, event, now=step.get("at"))
        assert state.name == want["state"], f"{where}: state"
        if "action" in want:
            assert intents, f"{where}: no action emitted"
            assert intents[0].kind == want["action"]["kind"], f"{where}: action kind"

    assert state.name == fx["expected"]["final_state"]
