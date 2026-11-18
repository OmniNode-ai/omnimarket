# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared-blocker escalation judge and andon-start guard (OMN-25406).

A shared cause that holds ``min_prs`` or more pull requests for ``hold_hours``, or whose measured
environment is not the gated branch's environment, or whose gated environment reads healthy while
the gate is red, raises ONE question to the operator. The question is keyed by the cause and its
pull request set, so a replay asks again only when the set changes. An andon-start row on a shared
check is refused unless it carries the environment the check measures and a reading of the gated
environment taken within the hour.

Pure: no clock (the caller supplies ``now``), no file read, no process.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from omnimarket.nodes.node_shared_blocker_escalation_compute.models.model_shared_blocker_escalation import (
    ModelAndonStartRequest,
    ModelAndonStartVerdict,
    ModelOperatorQuestion,
    ModelSharedBlockerRequest,
    ModelSharedBlockerResult,
    ModelSharedCause,
    ModelSharedCauseJudgement,
    Trigger,
)

OPTIONS: tuple[str, ...] = ("fix the gate", "admit by label", "keep waiting")
RECOMMEND = "fix the gate, because the gate's red blocks every member while the gated environment is the one to read"

MEASURES_ENV_CELL = "measures_env"
GATED_READING_CELL = "gated_env_reading"
_READING = re.compile(
    r"^(?P<env>[^:@\s]+):(?P<state>healthy|red)@(?P<at>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)$"
)


def question_key(cause_key: str, members: tuple[str, ...]) -> str:
    """The question's identity: the cause and its pull request set, so a changed set is a new question."""
    digest = hashlib.sha256("\n".join(sorted(members)).encode("utf-8")).hexdigest()[:12]
    return f"shared-cause:{cause_key}:{digest}"


def _triggers(
    c: ModelSharedCause, age_hours: float, req: ModelSharedBlockerRequest
) -> tuple[Trigger, ...]:
    out: list[Trigger] = []
    if len(c.members) >= req.min_prs and age_hours >= req.hold_hours:
        out.append("holds-prs-for-hours")
    if c.measured_env != c.gated_env:
        out.append("measures-other-env")
    if c.gated_reading == "healthy":
        out.append("gated-env-healthy")
    return tuple(out)


def _question(
    c: ModelSharedCause, qkey: str, age_hours: float, triggers: tuple[Trigger, ...]
) -> ModelOperatorQuestion:
    why = ", ".join(triggers)
    text = (
        f"The shared check {c.check} holds {len(c.members)} pull requests for {age_hours:.1f} h "
        f"(triggers: {why}); it measures {c.measured_env}, the gated branch runs on {c.gated_env}, "
        f"and {c.gated_env} reads {c.gated_reading}. Fix the gate, admit the pull requests by label, or keep waiting?"
    )
    return ModelOperatorQuestion(
        qkey=qkey,
        question=text,
        options=OPTIONS,
        recommend=RECOMMEND,
        members=tuple(sorted(c.members)),
        alarm_key=f"alarm:{qkey}",
    )


class HandlerSharedBlockerEscalation:
    """COMPUTE handler: shared causes and open questions in, judgements, questions and status lines out."""

    def handle(self, request: ModelSharedBlockerRequest) -> ModelSharedBlockerResult:
        open_keys = set(request.open_questions)
        judgements: list[ModelSharedCauseJudgement] = []
        for c in request.causes:
            age_hours = max((request.now - c.first_seen).total_seconds(), 0.0) / 3600.0
            triggers = _triggers(c, age_hours, request)
            qkey = question_key(c.key, c.members)
            is_open = qkey in open_keys
            ask = (
                _question(c, qkey, age_hours, triggers)
                if triggers and not is_open
                else None
            )
            judgements.append(
                ModelSharedCauseJudgement(
                    cause_key=c.key,
                    check=c.check,
                    age_hours=round(age_hours, 2),
                    pr_count=len(c.members),
                    triggers=triggers,
                    qkey=qkey,
                    question_open=is_open,
                    ask=ask,
                )
            )
        return ModelSharedBlockerResult(
            judgements=tuple(judgements),
            questions=tuple(j.ask for j in judgements if j.ask is not None),
            status_lines=tuple(render_status_line(j) for j in judgements),
        )


def render_status_line(j: ModelSharedCauseJudgement) -> str:
    """One queue-status line: the cause, its age in hours, its PR count and whether its question is open."""
    if j.question_open:
        state = "open"
    elif j.ask is not None:
        state = "due"
    else:
        state = "none"
    return f"shared-cause {j.check} | age={j.age_hours:.1f}h | prs={j.pr_count} | question={state}"


def _cells(row_text: str) -> dict[str, str]:
    cells: dict[str, str] = {}
    for part in row_text.split(" | "):
        key, sep, value = part.partition("=")
        if sep and re.fullmatch(r"[A-Za-z_][\w-]*", key.strip()):
            cells.setdefault(key.strip(), value.strip())
    return cells


class HandlerAndonStartGuard:
    """COMPUTE handler: an andon-start row on a shared check needs the measured environment and a fresh gated reading."""

    def handle(self, request: ModelAndonStartRequest) -> ModelAndonStartVerdict:
        cells = _cells(request.row_text)
        blocker = cells.get("blocker", "").lower()
        shared = {s.lower() for s in request.shared_checks}
        if cells.get("andon") != "andon-start" or blocker not in shared:
            return ModelAndonStartVerdict(
                admitted=True, on_shared_check=False, missing=(), reasons=()
            )
        missing: list[str] = []
        reasons: list[str] = []
        if not cells.get(MEASURES_ENV_CELL):
            missing.append(MEASURES_ENV_CELL)
            reasons.append(
                f"names no {MEASURES_ENV_CELL}: the environment the check measures"
            )
        reading = cells.get(GATED_READING_CELL)
        if not reading:
            missing.append(GATED_READING_CELL)
            reasons.append(
                f"carries no {GATED_READING_CELL}: a reading of {request.gated_env} taken within the hour"
            )
        else:
            m = _READING.match(reading)
            if m is None:
                missing.append(GATED_READING_CELL)
                reasons.append(
                    f"{GATED_READING_CELL} is not <env>:<healthy|red>@<ISO-Z>"
                )
            elif m["env"] != request.gated_env:
                missing.append(GATED_READING_CELL)
                reasons.append(
                    f"{GATED_READING_CELL} reads {m['env']}, not the gated environment {request.gated_env}"
                )
            else:
                taken = datetime.fromisoformat(m["at"].replace("Z", "+00:00"))
                age = request.now - taken
                if age > timedelta(
                    minutes=request.max_reading_age_minutes
                ) or age < timedelta(0):
                    missing.append(GATED_READING_CELL)
                    reasons.append(
                        f"{GATED_READING_CELL} was taken at {m['at']}, outside {request.max_reading_age_minutes} minutes of now"
                    )
        return ModelAndonStartVerdict(
            admitted=not missing,
            on_shared_check=True,
            missing=tuple(missing),
            reasons=tuple(reasons),
        )


__all__ = [
    "HandlerAndonStartGuard",
    "HandlerSharedBlockerEscalation",
    "question_key",
    "render_status_line",
]
