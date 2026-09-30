# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: one synthetic row per canonical ledger row type (OMN-19513)."""

from __future__ import annotations

import pytest

# One row per canonical type, in the grammar's shape. Synthetic on purpose: the
# fixtures name no real lane, ticket or operator words.
ROWS: dict[str, str] = {
    "CLAIM": "2026-09-28T10:00:00Z | CLAIM | lane=alpha-1 | ticket=OMN-1 | actor=claude:sonnet5:subagent | repo=omnimarket | est ~1 lane-hours; displaces nothing; (OMN-1) | do the thing",
    "STATUS": "2026-09-28T10:05:00Z | STATUS | lane=alpha-1 | ticket=OMN-1 | repo=omnimarket | pr=omnimarket#7 | head=abc123 | state=green | a status line",
    "TERMINAL": "2026-09-28T10:30:00Z | TERMINAL | lane=alpha-1 | ticket=OMN-1 | outcome=handed-off | pr=omnimarket#7 | friction=none | delegated=0 delegation_reason=none needed",
    "HOLD": "2026-09-28T10:10:00Z | HOLD | lane=beta-2 | id=2026-09-28T10:10:00Z-beta-2 | surface=lab-dev | until=2026-09-28T12:00:00Z | proof surface reserved",
    "RELEASE": "2026-09-28T11:00:00Z | RELEASE | lane=beta-2 | re=2026-09-28T10:10:00Z-beta-2 | surface=lab-dev | result=PASS | restored=yes | done",
    "MSG": "2026-09-28T10:15:00Z | MSG | from=alpha-1 | to=beta-2,gamma-3 | id=2026-09-28T10:15:00Z-alpha-1 | repo=omnimarket | pr=omnimarket#7 | please look",
    "ACK": "2026-09-28T10:16:00Z | ACK | from=beta-2 | to=alpha-1 | id=2026-09-28T10:16:00Z-beta-2 | re=2026-09-28T10:15:00Z-alpha-1",
    "RULING": '2026-09-28T09:00:00Z | RULING | lane=orch-1 | ticket=OMN-1 | amends=2026-09-27T09:00:00Z | amends=2026-09-27T09:05:00Z | "go do it" | ruling text',
    "OPERATOR-CONSENT": '2026-09-28T09:01:00Z | OPERATOR-CONSENT | lane=orch-1 | "yes" | APPROVED SCOPE: the lab lane | OUT OF SCOPE: production | This row is the durable authorization evidence',
    "FRICTION": "2026-09-28T10:20:00Z | FRICTION | lane=alpha-1 | ticket=OMN-2 | cost=20m | class=tooling | the hook was slow",
    "CORRECTION": "2026-09-28T10:25:00Z | CORRECTION | lane=alpha-1 | corrects=2026-09-28T10:05:00Z | the head was wrong",
}


@pytest.fixture
def rows() -> dict[str, str]:
    return dict(ROWS)


@pytest.fixture(autouse=True)
def _spool_only_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """No broker in unit tests: the declared spool-only opt-out (OMN-16167)."""
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_ONLY", "true")
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
