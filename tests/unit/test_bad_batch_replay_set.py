# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bad-batch replay set is verified against the ledger rows it was read from.

The rebuild plan (section 3.5 row 13) quotes "the 14 Codex lanes that died of a
full disk between 19:23Z and 19:26Z on 11-04" and calls the source unread. The
fixture holds what the rolling ledger shows for that window on host h051: every
death with the ledger row (file and line) and the identifiers of its run, and
the corrected count. These tests check the fixture against itself, so they run
on any runner with no ledger present.

OMN-22968 AC1 (the matched_or_corrected tests) and AC2 (the replay tests): the
replay feeds each death through the lane result sweep node's own ``classify``
with the vocabulary its overlay example ships, and a planted misclassification
must be reported.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_lane_result_sweep_effect as sweep_node
from omnimarket.models.model_lane_result_sweep import (
    EnumLaneResultClass,
    ModelLaneReceipt,
)
from omnimarket.nodes.node_lane_result_sweep_effect.handlers.handler_lane_result_sweep import (
    classify,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "bad_batch_replay"
    / "h051_2026_11_04_1923z.json"
)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TS = re.compile(r"^2026-11-04T\d\d:\d\d:\d\dZ$")


def _load() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return doc


def verify_set(doc: dict[str, Any]) -> list[str]:
    """Return every reason the set is not matched-or-corrected; empty when it is."""
    problems: list[str] = []
    source = doc["source"]
    verdict = doc["verdict"]
    deaths = doc["deaths"]
    start, end = source["window_start"], source["window_end"]

    for key in ("run", "codex_thread", "req", "lane"):
        values = [d[key] for d in deaths]
        if len(set(values)) != len(values):
            problems.append(f"duplicate {key} among the deaths")

    for d in deaths:
        name = d.get("lane", "?")
        if not _TS.match(d["ts"]) or not (start <= d["ts"] <= end):
            problems.append(f"{name}: ts {d['ts']} outside the window")
        if d.get("shape") not in ("engine-died", "engine-ok-landing-failed"):
            problems.append(f"{name}: no shape")
        elif (d["engine_rc"] != 0) != (d["shape"] == "engine-died"):
            problems.append(f"{name}: shape does not follow engine_rc")
        for key in ("run", "codex_thread", "closes_claim", "ledger_file"):
            if not d.get(key):
                problems.append(f"{name}: {key} empty")
        if not _UUID.match(str(d.get("req", ""))):
            problems.append(f"{name}: req is not a ledger request id")
        if not isinstance(d.get("ledger_line"), int) or d["ledger_line"] < 1:
            problems.append(f"{name}: no ledger line")
        if not d.get("status_row"):
            problems.append(f"{name}: no paired STATUS row")

    quoted, corrected = source["quoted_count"], verdict["corrected_count"]
    if corrected != len(deaths):
        problems.append(f"corrected_count {corrected} != {len(deaths)} deaths listed")
    nonzero = sum(1 for d in deaths if d["engine_rc"] != 0)
    if verdict["engine_nonzero_count"] != nonzero:
        problems.append(
            f"engine_nonzero_count {verdict['engine_nonzero_count']} != {nonzero}"
        )
    if quoted != corrected:
        if not verdict.get("note"):
            problems.append("the count differs from the quote and no note says why")
        if "unmatched_quoted" not in verdict:
            problems.append("the count differs and unmatched_quoted is not listed")
    elif verdict.get("unmatched_quoted"):
        problems.append("the count matches the quote but unmatched entries are listed")

    for adj in doc["adjacent_outside_window"]:
        if start <= adj["ts"] <= end:
            problems.append(f"adjacent {adj['lane']} is inside the window")

    if not verdict["cause_attested_per_lane"] and not doc["cause_evidence"]:
        problems.append("no cause evidence at all")
    for ev in doc["cause_evidence"]:
        if not (ev.get("ledger_line") and _UUID.match(str(ev.get("req", "")))):
            problems.append(f"cause evidence {ev.get('lane')} has no row reference")
    return problems


def test_matched_or_corrected_set_is_clean() -> None:
    assert verify_set(_load()) == []


def test_matched_or_corrected_count_is_not_the_quoted_fourteen() -> None:
    doc = _load()
    assert doc["source"]["quoted_count"] == 14
    assert doc["verdict"]["corrected_count"] == len(doc["deaths"]) == 16
    assert doc["verdict"]["engine_nonzero_count"] == 14
    assert doc["verdict"]["unmatched_quoted"] == []
    assert doc["verdict"]["quoted_entries_individually_named"] is False


def test_matched_or_corrected_every_death_is_a_failed_h051_codex_lane() -> None:
    doc = _load()
    for d in doc["deaths"]:
        assert d["run"].startswith(f"rlane-{d['lane']}-")
        assert d["status_row"]["status"] in ("lane-error", "landing-failed")


def test_matched_or_corrected_host_cause_is_not_claimed_per_lane() -> None:
    doc = _load()
    assert doc["verdict"]["cause_attested_per_lane"] is False
    assert {e["lane"] for e in doc["cause_evidence"]} == {
        "disk-triage-1955",
        "h051-sandbox-claude-2055",
    }


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d["deaths"].pop(), "corrected_count"),
        (lambda d: d["verdict"].update(corrected_count=14), "corrected_count"),
        (
            lambda d: d["verdict"].update(engine_nonzero_count=16),
            "engine_nonzero_count",
        ),
        (
            lambda d: d["deaths"][0].update(ts="2026-11-04T19:40:00Z"),
            "outside the window",
        ),
        (lambda d: d["deaths"][1].update(run=d["deaths"][0]["run"]), "duplicate run"),
        (lambda d: d["deaths"][2].update(req="not-a-request-id"), "req is not"),
        (lambda d: d["deaths"][3].update(status_row=None), "no paired STATUS"),
        (lambda d: d["verdict"].pop("note"), "no note says why"),
        (
            lambda d: d["verdict"].pop("unmatched_quoted"),
            "unmatched_quoted is not listed",
        ),
        (lambda d: d["cause_evidence"].clear(), "no cause evidence"),
    ],
)
def test_matched_or_corrected_fails_on_a_planted_break(
    mutate: Any, expected: str
) -> None:
    broken = copy.deepcopy(_load())
    mutate(broken)
    problems = verify_set(broken)
    assert any(expected in p for p in problems), problems


Classifier = Callable[
    [
        ModelLaneReceipt,
        Mapping[str, EnumLaneResultClass],
        Mapping[str, EnumLaneResultClass],
    ],
    EnumLaneResultClass,
]


def _vocabulary(name: str) -> dict[str, EnumLaneResultClass]:
    """One token table of the sweep node's shipped overlay example."""
    example = Path(sweep_node.__file__).parent / "overlay.example.yaml"
    table = yaml.safe_load(example.read_text(encoding="utf-8"))[name]
    return {token: EnumLaneResultClass(cls) for token, cls in table.items()}


def _receipt_of(doc: dict[str, Any], death: dict[str, Any]) -> ModelLaneReceipt:
    return ModelLaneReceipt.model_validate(
        {
            "lane": death["lane"],
            "finished_at": death["ts"],
            "terminal_outcome": doc["source"]["terminal_outcome"],
            "engine_rc": death["engine_rc"],
        }
    )


def replay(doc: dict[str, Any], classifier: Classifier) -> list[str]:
    """Classify every death of the set; each one that does not read failed is listed."""
    outcomes = _vocabulary("outcome_classes")
    results = _vocabulary("result_classes")
    wrong: list[str] = []
    for death in doc["deaths"]:
        got = classifier(_receipt_of(doc, death), outcomes, results)
        if got is not EnumLaneResultClass.FAILED:
            wrong.append(f"{death['lane']}: read {got.value}, not failed")
    return wrong


def test_replay_classifies_every_death_failed() -> None:
    doc = _load()
    assert len(doc["deaths"]) == 16
    assert replay(doc, classify) == []


def test_replay_reads_a_handed_off_claim_over_a_dead_engine_as_failed() -> None:
    doc = _load()
    for death in doc["deaths"]:
        if death["engine_rc"] == 0:
            continue
        claimed = _receipt_of(doc, death).model_copy(
            update={"terminal_outcome": "handed-off"}
        )
        got = classify(claimed, _vocabulary("outcome_classes"), {})
        assert got is EnumLaneResultClass.FAILED, death["lane"]


def _never_failed(
    receipt: ModelLaneReceipt,
    outcomes: Mapping[str, EnumLaneResultClass],
    results: Mapping[str, EnumLaneResultClass],
) -> EnumLaneResultClass:
    return EnumLaneResultClass.DONE


def _failed_only_on_a_nonzero_exit(
    receipt: ModelLaneReceipt,
    outcomes: Mapping[str, EnumLaneResultClass],
    results: Mapping[str, EnumLaneResultClass],
) -> EnumLaneResultClass:
    if receipt.engine_rc not in (None, 0):
        return EnumLaneResultClass.FAILED
    return EnumLaneResultClass.PARTIAL


@pytest.mark.parametrize(
    ("classifier", "wrong_count"),
    [(_never_failed, 16), (_failed_only_on_a_nonzero_exit, 2)],
)
def test_replay_fails_on_misclass(classifier: Classifier, wrong_count: int) -> None:
    wrong = replay(_load(), classifier)
    assert len(wrong) == wrong_count, wrong


def test_replay_fails_on_misclass_through_the_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vocabulary that maps the dead lanes' ``failed`` token to done is caught."""
    real = _vocabulary

    def broken(name: str) -> dict[str, EnumLaneResultClass]:
        table = real(name)
        if name == "outcome_classes":
            table["failed"] = EnumLaneResultClass.DONE
        return table

    monkeypatch.setattr(f"{__name__}._vocabulary", broken)
    zero_exit = [d for d in _load()["deaths"] if d["engine_rc"] == 0]
    assert len(zero_exit) == 2
    assert len(replay(_load(), classify)) == len(zero_exit)
