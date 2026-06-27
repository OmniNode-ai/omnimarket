# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_worker_stall_recovery.

WS-5 Wave 7 (OMN-13681). EFFECT archetype -> Variant A: the async handler is
invoked in-process and the typed result dict (status / stall_reason /
redispatch_count) is asserted.

The I/O boundary is the ``.onex_state`` tree (dispatch-log JSONL files,
dispatches specs, checkpoints, friction reports). Each case builds a REAL
synthetic tree under ``tmp_path`` and points ``OMNI_HOME`` at it — the handler's
``grep`` and file reads run against genuine fixtures, so nothing about
``subprocess`` or ``asyncpg`` is monkeypatched (per scout §7.3).

Param axes (>=3 distinct sets + negative controls):
  * recent activity -> "healthy".
  * agent absent from the dispatch log -> "healthy" w/ agent_not_found reason.
  * stale activity + dry_run -> "stalled"            (NEGATIVE CONTROL: finding).
  * recent activity over the context-% threshold + dry_run -> "stalled".
  * stale + a recoverable dispatch spec -> "recovered" (redispatch_count == 1).
  * stale + no dispatch spec -> "escalated" + friction report written.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_worker_stall_recovery.handlers.handler_stall_recovery import (
    HandlerStallRecovery,
)
from omnimarket.nodes.node_worker_stall_recovery.models.model_stall_recovery_command import (
    ModelStallRecoveryCommand,
)

_AGENT = "agent-wave7"
_TICKET = "OMN-13681"


def _iso(minutes_ago: float) -> str:
    return (datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _build_state(
    tmp_path: Path,
    *,
    log_agent: str,
    minutes_ago: float,
    context_pct: int | None,
    with_dispatch_spec: bool,
) -> None:
    """Materialize a real .onex_state tree under OMNI_HOME=tmp_path."""
    onex_state = tmp_path / ".onex_state"
    log_dir = onex_state / "dispatch-log"
    log_dir.mkdir(parents=True, exist_ok=True)
    event: dict[str, Any] = {"agent_id": log_agent, "timestamp": _iso(minutes_ago)}
    if context_pct is not None:
        event["context_pct"] = context_pct
    (log_dir / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    if with_dispatch_spec:
        dispatches = onex_state / "dispatches"
        dispatches.mkdir(parents=True, exist_ok=True)
        (dispatches / f"{_AGENT}.json").write_text(
            json.dumps({"agent_id": _AGENT, "ticket_id": _TICKET}),
            encoding="utf-8",
        )


# (case_id, build_kwargs, command_kwargs, expect)
_CASES: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = [
    (
        "healthy-recent-activity",
        {
            "log_agent": _AGENT,
            "minutes_ago": 0.0,
            "context_pct": None,
            "with_dispatch_spec": False,
        },
        {"dry_run": True},
        {"status": "healthy", "reason_exact": "", "redispatch_count": 0},
    ),
    (
        "agent-not-in-dispatch-log",
        {
            "log_agent": "some-other-agent",
            "minutes_ago": 0.0,
            "context_pct": None,
            "with_dispatch_spec": False,
        },
        {"dry_run": True},
        {
            "status": "healthy",
            "reason_exact": "agent_not_found_in_dispatch_log",
            "redispatch_count": 0,
        },
    ),
    (
        "stalled-inactivity-dry-run",  # NEGATIVE CONTROL
        {
            "log_agent": _AGENT,
            "minutes_ago": 60.0,
            "context_pct": None,
            "with_dispatch_spec": False,
        },
        {"dry_run": True, "timeout_minutes": 2},
        {"status": "stalled", "reason_prefix": "inactivity_", "redispatch_count": 0},
    ),
    (
        "stalled-context-threshold-dry-run",
        {
            "log_agent": _AGENT,
            "minutes_ago": 0.0,
            "context_pct": 95,
            "with_dispatch_spec": False,
        },
        {"dry_run": True, "context_threshold_pct": 90},
        {
            "status": "stalled",
            "reason_exact": "context_pct_95_exceeds_90",
            "redispatch_count": 0,
        },
    ),
    (
        "stalled-recovered",
        {
            "log_agent": _AGENT,
            "minutes_ago": 60.0,
            "context_pct": None,
            "with_dispatch_spec": True,
        },
        {"dry_run": False, "timeout_minutes": 2, "max_redispatches": 2},
        {"status": "recovered", "reason_prefix": "inactivity_", "redispatch_count": 1},
    ),
    (
        "stalled-escalated",
        {
            "log_agent": _AGENT,
            "minutes_ago": 60.0,
            "context_pct": None,
            "with_dispatch_spec": False,
        },
        {"dry_run": False, "timeout_minutes": 2, "max_redispatches": 1},
        {"status": "escalated", "reason_prefix": "inactivity_", "redispatch_count": 0},
    ),
]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "build_kwargs", "command_kwargs", "expect"),
    _CASES,
    ids=[c[0] for c in _CASES],
)
async def test_stall_recovery_multiparam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    build_kwargs: dict[str, Any],
    command_kwargs: dict[str, Any],
    expect: dict[str, Any],
) -> None:
    _build_state(tmp_path, **build_kwargs)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))

    handler = HandlerStallRecovery()
    result = await handler.handle(
        ModelStallRecoveryCommand(
            ticket_id=_TICKET,
            agent_id=_AGENT,
            **command_kwargs,
        )
    )

    assert result["status"] == expect["status"], f"{case_id}: {result}"
    assert result["redispatch_count"] == expect["redispatch_count"]

    if "reason_exact" in expect:
        assert result["stall_reason"] == expect["reason_exact"]
    if "reason_prefix" in expect:
        assert result["stall_reason"].startswith(expect["reason_prefix"])

    if expect["status"] in ("stalled", "recovered", "escalated"):
        assert result["checkpoint_path"], f"{case_id}: stall paths carry a checkpoint"

    if expect["status"] == "escalated":
        # The escalation path must durably record a friction report.
        friction = tmp_path / ".onex_state" / "friction"
        reports = list(friction.glob("*agent-stall-escalation*.md"))
        assert reports, f"{case_id}: escalation must write a friction report"
