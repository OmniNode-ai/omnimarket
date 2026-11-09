# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared test data for the beta board: a two-criterion overlay and scripted readers. Every id here is a test id."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_beta_board_effect.handlers.handler_beta_board import (
    OWN_CONTRACT,
    load_runtime,
)
from omnimarket.nodes.node_beta_board_effect.models.model_beta_board import (
    ModelBetaBoardOverlay,
    ModelBetaBoardRuntime,
    ModelReadingPoint,
    ModelSourceRead,
    ReadingVerdict,
    SourceKind,
)

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)

OVERLAY = {
    "milestone_id": "MX",
    "milestone_title": "Test milestone",
    "cloud_repositories": ["Org/cloudrepo"],
    "serve_targets": [
        {
            "name": "host-a",
            "ssh_target": "alias-a",
            "remote_dir": "/srv/a",
            "serve_url": "http://a/beta.html",
        },
        {
            "name": "host-b",
            "ssh_target": "alias-b",
            "remote_dir": "/srv/b",
            "serve_url": "http://b/beta.html",
        },
    ],
    "criteria": [
        {
            "id": "T1",
            "condition": "first condition",
            "proof": "first proof",
            "owner_ticket": "OMN-1",
            "carriers": [{"ticket": "OMN-1"}, {"ticket": "OMN-2"}],
            "probes": ["probe-local"],
        },
        {
            "id": "T2",
            "condition": "second condition",
            "proof": "second proof",
            "owner_ticket": "OMN-3",
            "carriers": [{"ticket": "OMN-3"}],
            "probes": ["probe-cloud"],
            "ledger_readings": ["ledger-one"],
        },
        {
            "id": "T3",
            "condition": "third condition",
            "proof": "third proof",
            "owner_ticket": "OMN-4",
        },
    ],
    "probes": [
        {
            "id": "probe-local",
            "repository": "Org/localrepo",
            "workflow": "local.yml",
            "max_age_seconds": 7200,
        },
        {
            "id": "probe-cloud",
            "repository": "Org/cloudrepo",
            "workflow": "cloud.yml",
            "max_age_seconds": 7200,
        },
    ],
    "ledger_readings": [
        {
            "id": "ledger-one",
            "row_type": "STATUS",
            "cells": {"src": "thing"},
            "verdict_cell": "verdict",
            "pass_values": ["WORKING"],
            "fail_values": ["NOT_WORKING"],
            "time_cell": "probed_at",
            "max_age_seconds": 7200,
            "history_seconds": 86400,
        },
    ],
    "ledger_read": {
        "route": "file",
        "path_env": "TEST_LEDGER_FILE",
        "timeout_seconds": 30,
        "row_limit": 1000,
    },
}


def overlay() -> ModelBetaBoardOverlay:
    return ModelBetaBoardOverlay.model_validate(OVERLAY)


def runtime() -> ModelBetaBoardRuntime:
    return load_runtime(OWN_CONTRACT, environ={}, overlay=overlay())


def ago(**delta: float) -> datetime:
    return NOW - timedelta(**delta)


def read(
    source_id: str, kind: SourceKind, *points: tuple[datetime, ReadingVerdict]
) -> ModelSourceRead:
    return ModelSourceRead(
        source_id=source_id,
        kind=kind,
        read="read",
        read_at=NOW,
        points=tuple(
            ModelReadingPoint(at=at, verdict=v, ref=f"ref-{i}")
            for i, (at, v) in enumerate(points)
        ),
    )


def run_list_answer(runs: list[tuple[str, str, datetime]]) -> str:
    return json.dumps(
        {
            "verb": "run-list",
            "outcome": "completed",
            "result": {
                "runs": [
                    {
                        "id": i,
                        "status": status,
                        "conclusion": conclusion,
                        "event": "schedule",
                        "created_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "updated_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "html_url": f"https://example.invalid/run/{i}",
                    }
                    for i, (status, conclusion, at) in enumerate(runs)
                ]
            },
        }
    )


def write_ledger(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
