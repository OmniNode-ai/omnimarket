# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared-blocker escalation and the andon-start guard (OMN-25406), replayed over a recorded slice."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_shared_blocker_escalation_compute.handlers.handler_shared_blocker_escalation import (
    HandlerAndonStartGuard,
    HandlerSharedBlockerEscalation,
)
from omnimarket.nodes.node_shared_blocker_escalation_compute.models.model_shared_blocker_escalation import (
    ModelAndonStartRequest,
    ModelSharedBlockerRequest,
    ModelSharedBlockerResult,
    ModelSharedCause,
)

FIXTURE = Path(__file__).parent / "fixtures" / "recorded_shared_blocker_ticks.json"
CHECK = "delegation-health-check / Delegation Health Check"


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _slice() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _replay() -> list[tuple[datetime, ModelSharedBlockerResult]]:
    """Feed every recorded tick to the judge, keeping each question it raises open for the next tick."""
    handler = HandlerSharedBlockerEscalation()
    open_questions: list[str] = []
    out: list[tuple[datetime, ModelSharedBlockerResult]] = []
    for tick in _slice()["ticks"]:
        now = _ts(tick["ts"])
        result = handler.handle(
            ModelSharedBlockerRequest(
                now=now,
                causes=tuple(
                    ModelSharedCause.model_validate(
                        {**c, "first_seen": _ts(c["first_seen"])}
                    )
                    for c in tick["causes"]
                ),
                open_questions=tuple(open_questions),
            )
        )
        open_questions.extend(q.qkey for q in result.questions)
        out.append((now, result))
    return out


def _cause(**over: Any) -> ModelSharedCause:
    base: dict[str, Any] = {
        "key": "cause:x/y:aaaa",
        "check": "Shared Check",
        "members": tuple(f"y#{n}" for n in range(1, 6)),
        "first_seen": datetime(2026, 11, 17, 0, 0, tzinfo=UTC),
        "measured_env": "dev",
        "gated_env": "dev",
        "gated_reading": "red",
    }
    base.update(over)
    return ModelSharedCause.model_validate(base)


def _judge(
    cause: ModelSharedCause, now: datetime, open_questions: tuple[str, ...] = ()
) -> ModelSharedBlockerResult:
    return HandlerSharedBlockerEscalation().handle(
        ModelSharedBlockerRequest(
            now=now, causes=(cause,), open_questions=open_questions
        )
    )


# ---- AC1 ---------------------------------------------------------------------------------------


def _andon(row: str, now: str = "2026-11-17T12:37:06Z") -> Any:
    return HandlerAndonStartGuard().handle(
        ModelAndonStartRequest(
            row_text=row, now=_ts(now), shared_checks=("delegation-health-check",)
        )
    )


def test_andon_guarded_env_refuses_the_recorded_row_and_admits_it_with_both_cells() -> (
    None
):
    row = _slice()["andon_start_row"]
    verdict = _andon(row)
    assert not verdict.admitted
    assert verdict.on_shared_check
    assert verdict.missing == ("measures_env", "gated_env_reading")
    # The row's own free text (the last cell) is untouched; both cells go in beside the key=value cells.
    head, _, text = row.rpartition(" | ")
    fixed = f"{head} | measures_env=stability | gated_env_reading=dev:healthy@2026-11-17T12:30:00Z | {text}"
    admitted = _andon(fixed)
    assert admitted.admitted
    assert admitted.missing == ()


@pytest.mark.parametrize(
    ("reading", "why"),
    [
        ("dev:healthy@2026-11-17T11:00:00Z", "outside 60 minutes"),
        ("stability:red@2026-11-17T12:30:00Z", "not the gated environment"),
        ("dev:healthy", "not <env>"),
        ("dev:healthy@2026-11-17T13:30:00Z", "outside 60 minutes"),
    ],
)
def test_andon_guarded_env_refuses_a_stale_or_wrong_reading(
    reading: str, why: str
) -> None:
    row = f"2026-11-17T12:37:06Z | STATUS | andon=andon-start | blocker=delegation-health-check | measures_env=stability | gated_env_reading={reading} | text"
    verdict = _andon(row)
    assert not verdict.admitted
    assert verdict.missing == ("gated_env_reading",)
    assert why in verdict.reasons[0]


def test_andon_guarded_env_leaves_other_rows_alone() -> None:
    assert _andon(
        "2026-11-17T12:37:06Z | STATUS | andon=andon-start | blocker=some-other-check | text"
    ).admitted
    assert _andon(
        "2026-11-17T12:37:06Z | STATUS | andon=andon-stop | blocker=delegation-health-check | text"
    ).admitted


# ---- AC2 ---------------------------------------------------------------------------------------


def test_shared_blocker_escalation_replay_asks_once_by_0430() -> None:
    replay = _replay()
    asked = [(now, q) for now, r in replay for q in r.questions]
    by_0430 = [q for now, q in asked if now <= _ts("2026-11-17T04:30:00Z")]
    assert len(by_0430) == 1
    assert (
        len(asked) == 1
    )  # the set never changes in the slice, so nothing asks again after 04:30Z
    question = by_0430[0]
    assert question.to == "operator"
    assert question.options == ("fix the gate", "admit by label", "keep waiting")
    assert question.alarm_key.startswith("alarm:shared-cause:")
    assert len(question.members) == 7


def test_shared_blocker_escalation_names_the_trigger_of_the_recorded_cause() -> None:
    first = next(r for _, r in _replay() if r.questions)
    assert first.judgements[0].triggers == ("measures-other-env",)
    last = _replay()[-1][1].judgements[0]
    assert "gated-env-healthy" in last.triggers


def test_shared_blocker_escalation_asks_again_only_when_the_set_changes() -> None:
    now = datetime(2026, 11, 17, 5, 0, tzinfo=UTC)
    cause = _cause()
    first = _judge(cause, now)
    assert len(first.questions) == 1
    assert _judge(cause, now, (first.questions[0].qkey,)).questions == ()
    grown = _cause(members=(*cause.members, "y#6"))
    again = _judge(grown, now, (first.questions[0].qkey,))
    assert len(again.questions) == 1
    assert again.questions[0].qkey != first.questions[0].qkey
    reordered = _cause(members=tuple(reversed(cause.members)))
    assert _judge(reordered, now, (first.questions[0].qkey,)).questions == ()


def test_shared_blocker_escalation_thresholds() -> None:
    start = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
    held = _cause()
    assert _judge(held, start + timedelta(hours=1, minutes=59)).questions == ()
    assert len(_judge(held, start + timedelta(hours=2)).questions) == 1
    four = _cause(members=held.members[:4])
    assert _judge(four, start + timedelta(hours=9)).questions == ()
    assert len(_judge(_cause(measured_env="stability"), start).questions) == 1
    assert len(_judge(_cause(gated_reading="healthy"), start).questions) == 1


# ---- AC3 ---------------------------------------------------------------------------------------


def test_queue_status_shared_cause_age_prints_age_count_and_question_state() -> None:
    replay = _replay()
    asked_at, asked = next((now, r) for now, r in replay if r.questions)
    assert asked.status_lines == (
        f"shared-cause {CHECK} | age=0.2h | prs=7 | question=due",
    )
    assert asked_at == _ts("2026-11-17T02:30:00Z")
    now, last = replay[-1]
    assert now == _ts("2026-11-17T11:40:00Z")
    assert last.status_lines == (
        f"shared-cause {CHECK} | age=9.3h | prs=7 | question=open",
    )
    assert replay[0][1].status_lines == ()
