# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Nightly per-rung eval, first slice (OMN-19456).

AC1: one row per configured rung, task class and night, even for a rung that
cannot be reached. AC2: the .202 admission fixtures are committed and load.
AC3: a planted missing night is reported by the detector.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from omnimarket.delegation.rung_eval.cases import load_cases
from omnimarket.delegation.rung_eval.graders import (
    grade_classification,
    grade_grouping,
)
from omnimarket.delegation.rung_eval.missing_nights import detect_missing_nights
from omnimarket.delegation.rung_eval.models import (
    EnumRungEvalStatus,
    ModelRungSpec,
    ModelTransportResult,
)
from omnimarket.delegation.rung_eval.rows_store import read_rows, write_night
from omnimarket.delegation.rung_eval.rungs import load_rungs
from omnimarket.delegation.rung_eval.runner import RungUnresolvedError, run_night

pytestmark = pytest.mark.unit

NIGHT = date(2026, 9, 28)


def _cases() -> dict[str, object]:
    return {c.case_id: c for c in load_cases()}


def test_admission_fixtures_are_committed_and_complete() -> None:
    cases = _cases()
    assert set(cases) == {"pr_classification_37", "m4_ticket_grouping_29"}
    pr = cases["pr_classification_37"]
    assert pr.task_class == "classification"  # type: ignore[attr-defined]
    assert len(pr.answer_key) == 37  # type: ignore[attr-defined]
    m4 = cases["m4_ticket_grouping_29"]
    assert len(m4.ticket_ids) == 29  # type: ignore[attr-defined]


def _perfect_classification(case: object) -> str:
    return "\n".join(
        f"{pr} | {cls} | reason | action"
        for pr, cls in case.answer_key.items()  # type: ignore[attr-defined]
    )


def test_classification_grader_scores_fraction_and_passes_only_on_all() -> None:
    case = _cases()["pr_classification_37"]
    perfect = grade_classification(_perfect_classification(case), case.answer_key)  # type: ignore[attr-defined]
    assert perfect.passed
    assert perfect.score == 1.0
    lines = _perfect_classification(case).splitlines()
    lines[0] = lines[0].split(" | ")[0] + " | READY_NOW | wrong | none"
    one_wrong = grade_classification("\n".join(lines), case.answer_key)  # type: ignore[attr-defined]
    assert not one_wrong.passed
    assert one_wrong.score == pytest.approx(36 / 37)
    empty = grade_classification("", case.answer_key)  # type: ignore[attr-defined]
    assert empty.score == 0.0
    assert not empty.passed


def _grouping_answer(ids: list[str], park: list[str]) -> str:
    grouped = [i for i in ids if i not in park]
    lines = [
        f"GROUP 1 | four word label here | tickets: {', '.join(grouped)} | surface: x | unblocks: C8"
    ]
    lines.append("PARK:")
    lines += [f"{i} five word reason here" for i in park]
    return "\n".join(lines)


def test_grouping_grader_structural_checks() -> None:
    case = _cases()["m4_ticket_grouping_29"]
    ids = case.ticket_ids  # type: ignore[attr-defined]
    ok = grade_grouping(
        _grouping_answer(ids, ids[:2]),
        ids,
        case.known_ids,
        case.max_groups,  # type: ignore[attr-defined]
    )
    assert ok.passed, ok.detail
    dropped = grade_grouping(
        _grouping_answer(ids[1:], []),
        ids,
        case.known_ids,
        case.max_groups,  # type: ignore[attr-defined]
    )
    assert not dropped.passed
    invented = grade_grouping(
        _grouping_answer([*ids, "OMN-99999"], []),
        ids,
        case.known_ids,
        case.max_groups,  # type: ignore[attr-defined]
    )
    assert not invented.passed
    assert "OMN-99999" in invented.detail
    no_park = grade_grouping(
        "GROUP 1 | a b c d | tickets: "
        + ", ".join(ids)
        + " | surface: x | unblocks: C8",
        ids,
        case.known_ids,
        case.max_groups,  # type: ignore[attr-defined]
    )
    assert not no_park.passed


def test_rungs_config_names_every_configured_rung() -> None:
    ids = [r.rung_id for r in load_rungs()]
    assert ids[:3] == ["lab-201", "lab-202", "planner-200"]
    assert any(i.startswith("cloud-") for i in ids)
    assert len(ids) == len(set(ids))


class _Transport:
    def __init__(self, answers: dict[str, str], unresolved: set[str], broken: set[str]):
        self.answers, self.unresolved, self.broken = answers, unresolved, broken
        self.calls: list[tuple[str, str]] = []

    def complete(self, rung: ModelRungSpec, prompt: str) -> ModelTransportResult:
        self.calls.append((rung.rung_id, prompt))
        if rung.rung_id in self.unresolved:
            raise RungUnresolvedError(f"{rung.rung_id} endpoint env unset")
        if rung.rung_id in self.broken:
            raise ConnectionError("refused")
        return ModelTransportResult(
            text=self.answers[rung.rung_id], model_id=f"m-{rung.rung_id}", latency_ms=5
        )


def _spec(rung_id: str) -> ModelRungSpec:
    return ModelRungSpec(
        rung_id=rung_id,
        kind="local",
        base_url_env=f"X_{rung_id}_URL",
        model_env=f"X_{rung_id}_MODEL",
        api_key_env=None,
    )


def test_every_rung_and_case_gets_a_row_even_when_unreachable() -> None:
    cases = load_cases()
    rungs = [_spec("a"), _spec("b"), _spec("c")]
    pr = _cases()["pr_classification_37"]
    transport = _Transport(
        {"a": _perfect_classification(pr)}, unresolved={"b"}, broken={"c"}
    )
    rows = run_night(NIGHT, rungs, cases, transport)
    assert len(rows) == len(rungs) * len(cases)
    assert {(r.rung_id, r.case_id) for r in rows} == {
        (r.rung_id, c.case_id) for r in rungs for c in cases
    }
    by = {(r.rung_id, r.case_id): r for r in rows}
    assert by[("a", "pr_classification_37")].status is EnumRungEvalStatus.PASSED
    assert by[("a", "m4_ticket_grouping_29")].status is EnumRungEvalStatus.FAILED
    assert by[("b", "pr_classification_37")].status is EnumRungEvalStatus.UNRESOLVED
    assert by[("c", "pr_classification_37")].status is EnumRungEvalStatus.ERROR
    assert all(r.night == NIGHT for r in rows)
    # pinned: a rung is only ever sent to itself
    assert {c[0] for c in transport.calls} == {"a", "b", "c"}


def test_rows_round_trip_one_file_per_night(tmp_path: Path) -> None:
    cases = load_cases()
    rows = run_night(NIGHT, [_spec("a")], cases, _Transport({"a": ""}, set(), set()))
    path = write_night(tmp_path, NIGHT, rows)
    assert path.name == "rung_eval_2026-09-28.jsonl"
    assert len(path.read_text().splitlines()) == len(rows)
    assert json.loads(path.read_text().splitlines()[0])["night"] == "2026-09-28"
    assert write_night(tmp_path, NIGHT, rows) == path  # idempotent rewrite
    assert len(read_rows(tmp_path)) == len(rows)


def test_missing_night_detector_reports_a_planted_gap(tmp_path: Path) -> None:
    cases = load_cases()
    rungs = [_spec("a"), _spec("b")]
    t = _Transport({"a": "", "b": ""}, set(), set())
    for offset in range(3):
        night = NIGHT - timedelta(days=offset)
        rows = run_night(night, rungs, cases, t)
        if offset == 1:  # plant: night 09-27 lost every row of rung b
            rows = [r for r in rows if r.rung_id != "b"]
        if offset == 2:  # plant: night 09-26 absent entirely
            continue
        write_night(tmp_path, night, rows)
    gaps = detect_missing_nights(
        read_rows(tmp_path), rungs, cases, through=NIGHT, window_days=3
    )
    got = {(g.night, g.rung_id, g.case_id) for g in gaps}
    assert (date(2026, 9, 27), "b", "pr_classification_37") in got
    assert (date(2026, 9, 26), "a", "m4_ticket_grouping_29") in got
    assert not any(g.night == NIGHT for g in gaps)
    assert not any(g.night == date(2026, 9, 27) and g.rung_id == "a" for g in gaps)


def test_complete_window_reports_nothing(tmp_path: Path) -> None:
    cases = load_cases()
    rungs = [_spec("a")]
    t = _Transport({"a": ""}, set(), set())
    write_night(tmp_path, NIGHT, run_night(NIGHT, rungs, cases, t))
    assert (
        detect_missing_nights(
            read_rows(tmp_path), rungs, cases, through=NIGHT, window_days=1
        )
        == []
    )


def test_unset_api_key_is_unresolved_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.delegation.rung_eval.runner import OpenAIChatTransport

    rung = ModelRungSpec(
        rung_id="k",
        kind="cloud",
        base_url_env="K_URL",
        model_env="K_MODEL",
        api_key_env="K_KEY",
    )
    monkeypatch.setenv("K_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("K_MODEL", "m")
    monkeypatch.delenv("K_KEY", raising=False)
    with pytest.raises(RungUnresolvedError, match="K_KEY"):
        OpenAIChatTransport().complete(rung, "hi")
