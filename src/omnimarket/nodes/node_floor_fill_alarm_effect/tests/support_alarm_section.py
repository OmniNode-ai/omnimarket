# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay's ``floor_fill_alarm`` section the alarm tests bind: every path under ``tmp_path``, the append command the
real ledger writer (``support_ledger_grammar``)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from omnimarket.handlers.ledger_row_grammar import GOVERNED_LEDGER_NAMES
from omnimarket.nodes.node_floor_fill_alarm_effect.tests.support_ledger_grammar import (
    AUTHENTICATED_PRINCIPAL,
    REAL_WRITER_APPENDER,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support_live_tick import (
    SCOPE,
    SETTINGS_MAX_AGE_SECONDS,
)

MAX_RUN_AGE_MINUTES = 30


def alarm_section(tmp_path: Path) -> dict[str, Any]:
    appender = tmp_path / "real_writer_append.py"
    appender.write_text(REAL_WRITER_APPENDER, encoding="utf-8")
    ledger = tmp_path / sorted(GOVERNED_LEDGER_NAMES)[0]
    ledger.write_text("", encoding="utf-8")
    return {
        "scope": SCOPE,
        "row_lane": "floor-fill-alarm",
        "row_ticket": "OMN-24873",
        "row_principal": AUTHENTICATED_PRINCIPAL,
        "ledger_path": str(ledger),
        "ledger_append_command": [
            sys.executable,
            str(appender),
            str(ledger),
            AUTHENTICATED_PRINCIPAL,
            "{row}",
        ],
        "run_log_path": str(tmp_path / "alarm" / "runs.jsonl"),
        "check_log_path": str(tmp_path / "alarm" / "checks.jsonl"),
        "pool_snapshot_path": str(tmp_path / "pool.json"),
        "pool_record_suffix": ".read-at.json",
        "pool_snapshot_max_age_seconds": SETTINGS_MAX_AGE_SECONDS,
        "pool_engine": "claude",
        "max_run_age_minutes": MAX_RUN_AGE_MINUTES,
    }


__all__: list[str] = ["MAX_RUN_AGE_MINUTES", "alarm_section"]
