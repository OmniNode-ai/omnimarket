# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the reading tests: a small overlay, ledger rows and a fixed clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingOverlay

#: The window under test: 14:00 to 15:00 UTC, cutoff 15:10, run at 15:15.
START = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)
END = START + timedelta(hours=1)
CUTOFF = END + timedelta(minutes=10)
RUN_AT = datetime(2026, 11, 5, 15, 15, tzinfo=UTC)

PARITY_MAX_AGE = 129600
DELEGATION_MAX_AGE = 3600


def overlay_document() -> dict[str, Any]:
    """Three pieces: a state source, a liveness source and one with no producer today."""
    return {
        "ledger_read": {
            "route": "file",
            "path_env": "READING_TEST_LEDGER",
            "archive_subdir": "archive",
            "timeout_seconds": 30,
            "row_limit": 1000,
        },
        "ledger_append": {
            "command_env": "READING_TEST_APPEND",
            "host_env": "READING_TEST_HOST",
            "timeout_seconds": 30,
        },
        "window": {"length_seconds": 3600, "close_delay_seconds": 600},
        "publish": {
            "lane": "reading-writer",
            "actor": "node:node_reading_writer_effect",
            "publish_max_age_seconds": 7200,
            "reading_term": "src=reading",
        },
        "order": [1, 6, 15],
        "pieces": [
            {
                "piece": 1,
                "name": "code graph",
                "readable_today": False,
                "unknown_reason": "the nightly graph run does not exist yet",
                "injected_fault": "stop the timer and advance the clock",
            },
            {
                "piece": 6,
                "name": "ledger",
                "readable_today": True,
                "basis": "daily-parity-receipt",
                "sources": [
                    {
                        "source_id": "parity",
                        "row_type": "STATUS",
                        "lane": "work-ledger-parity",
                        "mode": "newest",
                        "pass_when": [
                            {"cell": "missing", "op": "eq", "value": "0"},
                            {"cell": "unexplained", "op": "eq", "value": "0"},
                        ],
                        "max_age_seconds": PARITY_MAX_AGE,
                    }
                ],
                "injected_fault": "an append returns with no durable row",
            },
            {
                "piece": 15,
                "name": "delegation",
                "readable_today": True,
                "basis": "delegation-terminal-rows",
                "sources": [
                    {
                        "source_id": "terminals",
                        "row_type": "STATUS",
                        "cells": {"src": "delegation", "outcome": "completed"},
                        "mode": "count",
                        "min_rows": 1,
                        "pass_when": [
                            {"cell": "quality_gate_passed", "op": "eq", "value": "true"}
                        ],
                        "max_age_seconds": DELEGATION_MAX_AGE,
                    }
                ],
                "injected_fault": "drop the result of the synthetic task in a test route",
            },
        ],
        "schedule_hosts": [{"name": "h-test", "scheduler": "systemd-user"}],
        "unit_environment": {"READING_TEST_APPEND": "/bin/true"},
    }


def overlay() -> ModelReadingOverlay:
    return ModelReadingOverlay.model_validate(overlay_document())


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parity_row(
    at: datetime,
    *,
    missing: int = 0,
    unexplained: int = 0,
    req: str = "p1",
    text: str = "parity",
) -> str:
    return (
        f"{stamp(at)} | STATUS | lane=work-ledger-parity | principal=dev-test | actor=script:work-ledger-parity"
        f" | model=none | day=2026-11-04 | missing={missing} | unexplained={unexplained}"
        f" | req={req} | via=bus:h200 | {text}"
    )


def delegation_row(
    at: datetime, *, outcome: str = "completed", passed: str = "true", req: str = "d1"
) -> str:
    return (
        f"{stamp(at)} | STATUS | lane=landing-L1 | principal=dev-test | src=delegation | run=r-{req}"
        f" | model=unreported | outcome={outcome} | quality_gate_passed={passed}"
        f" | req={req} | via=bus:delegation | Delegation terminal observed"
    )


def healthy_ledger() -> list[str]:
    """A ledger on which pieces 6 and 15 read WORKING for the window under test."""
    return [
        parity_row(START - timedelta(hours=10)),
        delegation_row(START + timedelta(minutes=20)),
    ]
