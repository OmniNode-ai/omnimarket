# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Routing learns from a task class only while its false-pass line is MET (OMN-19797, EV.9).

The per-class eval readout (``delegation_eval_results``, written by the EV.4 run)
is the one accuracy readout the routing read consults before it lets a stored
outcome suppress a tier. A class whose latest line is MISSED or REFUSED, a class
with no readout, and an unreadable readout all yield ``unknown``: the overlay
suppresses nothing and routing keeps the static tiers. Only MET lets the DoD
pass rate act.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from omnimarket.routing.dod_overlay import (
    EVAL_LINE_MET,
    ProtocolEvalLineReader,
    resolve_dod_overlay,
)

TENANT_ID = "820272f9-4aaf-5add-a2df-0af942852ab2"
TASK_TYPE = "document"
_T = datetime(2026, 9, 25, 3, 0, tzinfo=UTC)

pytestmark = pytest.mark.unit


def _failing_rows(n: int = 6) -> list[dict[str, object]]:
    return [
        {
            "task_type": TASK_TYPE,
            "correlation_id": str(uuid.uuid4()),
            "tier_name": "local",
            "model_name": "Qwen3.8-27B",
            "created_at": _T,
            "verdict_correlation_id": str(uuid.uuid4()),
            "verdict_status": "failed",
            "verdict_outcome": "refused",
            "verdict_completed_at": _T,
        }
        for _ in range(n)
    ]


class _DodReader:
    def read_dod_outcomes(
        self, *, task_type: str, tenant_id: str
    ) -> list[dict[str, object]]:
        return _failing_rows()


class _Line:
    def __init__(self, verdict: str | None) -> None:
        self.verdict = verdict
        self.calls: list[tuple[str, str]] = []

    def read_false_pass_line(self, *, task_class: str, tenant_id: str) -> str | None:
        self.calls.append((task_class, tenant_id))
        return self.verdict


class _BrokenLine:
    def read_false_pass_line(self, *, task_class: str, tenant_id: str) -> str | None:
        raise RuntimeError("relation delegation_eval_results does not exist")


def _resolve(line: ProtocolEvalLineReader | None):  # type: ignore[no-untyped-def]
    return resolve_dod_overlay(
        _DodReader(),
        task_type=TASK_TYPE,
        tenant_id=TENANT_ID,
        eval_line_reader=line,
        min_samples=5,
        success_floor=0.5,
        lookback_rows=None,
        window_seconds=None,
    )


@pytest.mark.parametrize("verdict", ["missed", "refused", "MISSED", "Refused"])
def test_eval_not_met_is_unknown(verdict: str) -> None:
    overlay = _resolve(_Line(verdict))
    assert overlay is not None
    assert overlay.roi_overlay.suppressed_tiers == frozenset()
    assert all(not s.suppressed for s in overlay.roi_overlay.signals)
    assert overlay.eval_line_verdict == verdict.lower()
    assert "eval_line=" in overlay.describe()


def test_eval_met_normal() -> None:
    line = _Line(EVAL_LINE_MET)
    overlay = _resolve(line)
    assert overlay is not None
    assert overlay.roi_overlay.suppressed_tiers == frozenset({"local"})
    assert overlay.eval_line_verdict == EVAL_LINE_MET
    assert line.calls == [(TASK_TYPE, TENANT_ID)]


def test_eval_unreadable_unknown() -> None:
    overlay = _resolve(_BrokenLine())
    assert overlay is not None
    assert overlay.roi_overlay.suppressed_tiers == frozenset()
    assert overlay.eval_line_verdict == "unreadable"


def test_eval_missing_row_unknown() -> None:
    overlay = _resolve(_Line(None))
    assert overlay is not None
    assert overlay.roi_overlay.suppressed_tiers == frozenset()
    assert overlay.eval_line_verdict == "missing"


def test_eval_no_reader_unknown() -> None:
    """No readout wired at all is the same as a missing readout: nothing suppressed."""
    overlay = _resolve(None)
    assert overlay is not None
    assert overlay.roi_overlay.suppressed_tiers == frozenset()
    assert overlay.eval_line_verdict == "missing"


def test_eval_gate_keeps_the_evidence_for_the_log() -> None:
    """Unknown clears suppression but keeps the counts the decision log cites."""
    overlay = _resolve(_Line("refused"))
    assert overlay is not None
    (signal,) = overlay.roi_overlay.signals
    assert signal.sample_count == 6
    assert signal.success_count == 0
    assert overlay.model_signals[0].sample_count == 6


def test_postgres_eval_line_reader_reads_the_latest_replayed_all_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.routing import dod_overlay

    executed: list[tuple[str, object]] = []

    class _Cursor:
        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute(self, sql: str, params: object = None) -> None:
            executed.append((sql, params))

        def fetchone(self) -> tuple[str] | None:
            return ("met",)

    class _Conn:
        closed = False
        autocommit = True

        def cursor(self) -> _Cursor:
            return _Cursor()

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    monkeypatch.setattr(
        "omnimarket.projection.postgres_read_database.connect_read_only",
        lambda *_args, **_kwargs: _Conn(),
    )
    reader = dod_overlay.PostgresEvalLineReader("postgresql://x")
    assert reader.read_false_pass_line(task_class=TASK_TYPE, tenant_id=TENANT_ID) == (
        "met"
    )
    set_config, select = executed
    assert set_config[1] == ("app.tenant_id", TENANT_ID)
    assert "delegation_eval_results" in select[0]
    assert "arm = 'replayed'" in select[0]
    assert "stratum = 'all'" in select[0]
    assert "ORDER BY observed_at DESC" in select[0]
    assert select[1] == {"tenant_id": TENANT_ID, "task_class": TASK_TYPE}


def test_eval_line_reader_resolution_needs_the_projection_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.routing import dod_overlay

    monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
    assert dod_overlay.resolve_eval_line_reader() is None
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://x")
    assert isinstance(
        dod_overlay.resolve_eval_line_reader(), dod_overlay.PostgresEvalLineReader
    )
