# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One alarm row per episode when the Temporal server or its worker is down, one recovery row when both are back.

The row shape is the deploy-job-failed alarm's (omnibase_infra ``handler_deploy_job_reading``): a ``STATUS`` row with
``src=alarm``, ``alarm=<name>``, the owner and the action the owner takes. Three facts are read on each tick:

* the server answers (``server-down`` when it does not);
* a worker is polling the task queue (``worker-not-polling`` when no poller is listed);
* the newest workflow task poll is under ``poll_stale_after_s`` old (``no-task-polled``, also when no poll is known).
  A poll exactly ``poll_stale_after_s`` old is stale.

The first unhealthy tick opens an episode in the state directory and appends the alarm row; later unhealthy ticks of
the same episode write nothing. The first healthy tick after it appends the recovery row and closes the episode. The
episode state is written only after its row was appended, so a row that could not be appended is tried again on the
next tick. A reading informs and refuses nothing: an append error is reported in the result, never raised.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_temporal_health_alarm_effect.models.model_temporal_health_alarm import (
    ModelTemporalHealthAlarmRequest,
    ModelTemporalHealthAlarmResult,
    ModelTemporalHealthFacts,
    ModelTemporalHealthSettings,
)

logger = logging.getLogger(__name__)

EPISODE_FILE = "episode.json"

# An appender takes one row and returns an error text, or "" when the row was appended.
Appender = Callable[[str], str]


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def evaluate(
    facts: ModelTemporalHealthFacts, settings: ModelTemporalHealthSettings
) -> tuple[str, ...]:
    """The reasons the reading is unhealthy, in a fixed order; empty when all three facts hold."""
    reasons: list[str] = []
    if not facts.server_answers:
        reasons.append("server-down")
    if not facts.worker_polling:
        reasons.append("worker-not-polling")
    if facts.newest_poll_at is None:
        reasons.append("no-task-polled")
    else:
        age_s = (facts.read_at - facts.newest_poll_at).total_seconds()
        if age_s >= settings.poll_stale_after_s:
            reasons.append("no-task-polled")
    return tuple(reasons)


def _head(settings: ModelTemporalHealthSettings, moment: datetime) -> list[str]:
    cells = [
        f"{_stamp(moment)} | STATUS",
        f"lane={settings.row_lane}",
        "src=alarm",
        f"actor={settings.row_actor}",
        "model=none",
        f"alarm={settings.alarm}",
    ]
    if settings.row_ticket:
        cells.append(f"ticket={settings.row_ticket}")
    return cells


def build_alarm_row(
    settings: ModelTemporalHealthSettings,
    request: ModelTemporalHealthAlarmRequest,
    reasons: tuple[str, ...],
    episode: str,
) -> str:
    facts = request.facts
    newest = _stamp(facts.newest_poll_at) if facts.newest_poll_at else "none"
    cells = [
        *_head(settings, facts.read_at),
        f"host={request.host}",
        "state=alarm",
        f"episode={episode}",
        f"reasons={','.join(reasons)}",
        f"newest_poll_at={newest}",
        f"owner={settings.alarm_owner}",
        f"owner_line={settings.alarm_owner_line}",
        f"Temporal on {request.host} is unhealthy ({', '.join(reasons)}); {facts.detail}".rstrip(
            "; "
        ),
    ]
    return " | ".join(cells)


def build_recovery_row(
    settings: ModelTemporalHealthSettings,
    request: ModelTemporalHealthAlarmRequest,
    episode: str,
) -> str:
    facts = request.facts
    newest = _stamp(facts.newest_poll_at) if facts.newest_poll_at else "none"
    cells = [
        *_head(settings, facts.read_at),
        f"host={request.host}",
        "state=recovered",
        f"episode={episode}",
        f"newest_poll_at={newest}",
        f"Temporal on {request.host} is healthy again: the server answers and a worker polled at {newest}",
    ]
    return " | ".join(cells)


def _read_episode(state_dir: Path) -> str:
    path = state_dir / EPISODE_FILE
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    episode = data.get("episode") if isinstance(data, dict) else None
    return episode if isinstance(episode, str) else ""


def _write_episode(state_dir: Path, episode: str, reasons: tuple[str, ...]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / f"{EPISODE_FILE}.tmp"
    tmp.write_text(
        json.dumps({"episode": episode, "reasons": list(reasons)}) + "\n",
        encoding="utf-8",
    )
    tmp.replace(state_dir / EPISODE_FILE)


class HandlerTemporalHealthAlarm:
    """Turn one reading into at most one alarm or recovery row."""

    def __init__(
        self, *, settings: ModelTemporalHealthSettings, append: Appender
    ) -> None:
        self._settings = settings
        self._append = append

    def handle(
        self, request: ModelTemporalHealthAlarmRequest
    ) -> ModelTemporalHealthAlarmResult:
        reasons = evaluate(request.facts, self._settings)
        episode = _read_episode(request.state_dir)
        if reasons:
            if episode:
                return ModelTemporalHealthAlarmResult(
                    verdict="unhealthy", reasons=reasons, episode=episode
                )
            episode = _stamp(request.facts.read_at)
            row = build_alarm_row(self._settings, request, reasons, episode)
            error = self._append(row)
            if error:
                logger.warning("temporal alarm row not appended: %s", error)
                return ModelTemporalHealthAlarmResult(
                    verdict="unhealthy",
                    reasons=reasons,
                    row_kind="alarm",
                    row=row,
                    outcome="unappended",
                    episode=episode,
                    error=error,
                )
            _write_episode(request.state_dir, episode, reasons)
            return ModelTemporalHealthAlarmResult(
                verdict="unhealthy",
                reasons=reasons,
                row_kind="alarm",
                row=row,
                outcome="appended",
                episode=episode,
            )
        if not episode:
            return ModelTemporalHealthAlarmResult(verdict="healthy")
        row = build_recovery_row(self._settings, request, episode)
        error = self._append(row)
        if error:
            logger.warning("temporal recovery row not appended: %s", error)
            return ModelTemporalHealthAlarmResult(
                verdict="healthy",
                row_kind="recovery",
                row=row,
                outcome="unappended",
                episode=episode,
                error=error,
            )
        (request.state_dir / EPISODE_FILE).unlink(missing_ok=True)
        return ModelTemporalHealthAlarmResult(
            verdict="healthy",
            row_kind="recovery",
            row=row,
            outcome="appended",
            episode=episode,
        )
