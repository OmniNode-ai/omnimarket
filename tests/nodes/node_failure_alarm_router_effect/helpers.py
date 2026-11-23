# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fakes for the failure alarm router tests (OMN-26483)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

ORCHESTRATOR = "orchestrator-31d9-swarm"
NOW = datetime(2026, 11, 22, 19, 0, tzinfo=UTC)
REFUSAL = "positive control failed: zero CLAIM rows parsed"


class FakePorts:
    """In-memory ports: appended rows are read back by the next fire, pushes are recorded."""

    def __init__(
        self,
        rows: list[str],
        ticks: list[str] | None = None,
        *,
        push_error: Exception | None = None,
    ) -> None:
        self.rows = list(rows)
        self.ticks = list(ticks or [])
        self.pushed: list[str] = []
        self.appends: list[tuple[str, list[str]]] = []
        self._push_error = push_error

    def read_rows(self) -> list[str]:
        return list(self.rows)

    def read_ticks(self) -> list[str]:
        return list(self.ticks)

    def append(self, request_id: str, rows: list[str]) -> None:
        self.appends.append((request_id, list(rows)))
        self.rows.extend(rows)

    def push(self, text: str) -> None:
        if self._push_error is not None:
            raise self._push_error
        self.pushed.append(text)


def tick(ts: str, n: int, *, refusal: str | None = None, status: str = "OK") -> str:
    row: dict[str, object] = {"ts": ts, "tick": n, "mode": "act", "status": status}
    if refusal is not None:
        row["refusal"] = refusal
    return json.dumps(row)


def orchestrator_claim(
    lane: str = ORCHESTRATOR, ts: str = "2026-11-22T08:00:00Z"
) -> str:
    return (
        f"{ts} | CLAIM | lane={lane} | actor=claude:opus:session | model=claude-opus"
        " | ticket=OMN-17427 | est ~8 lane-hours; displaces nothing; (OMN-17427) | the session lane"
    )


def base_rows() -> list[str]:
    return [
        orchestrator_claim(),
        "2026-11-22T18:30:00Z | CLAIM | lane=other-lane | actor=x | model=y | ticket=OMN-1"
        " | est ~1 lane-hours; displaces z; (OMN-1) | other",
    ]
