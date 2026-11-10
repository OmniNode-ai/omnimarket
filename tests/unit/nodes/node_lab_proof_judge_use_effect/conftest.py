# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fakes and fixtures for the lab proof judge use effect tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_lab_proof_judge_use_effect.overlay import (
    ModelLabProofJudgeUseOverlay,
)

_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_lab_proof_judge_use_effect/overlay.example.yaml"
)
# the topic the packaged overlay declares; no test spells it
USE_TOPIC: str = yaml.safe_load(_EXAMPLE.read_text())["event"]["topic"]


@dataclass
class FakeAppender:
    """In-memory appender that records rows and can be configured to fail or repeat."""

    rows: list[str] = field(default_factory=list)
    request_ids: list[str] = field(default_factory=list)
    fail: bool = False
    already_written: bool = False

    def append(self, row: str, request_id: str) -> Any:
        self.rows.append(row)
        self.request_ids.append(request_id)
        if self.fail:
            return _AppendResult(ok=False, status="rejected")
        if self.already_written:
            return _AppendResult(ok=True, status="already-written")
        return _AppendResult(ok=True, status="accepted")


@dataclass
class _AppendResult:
    ok: bool
    status: str


@dataclass
class FakePublisher:
    """In-memory publisher that records events and can be configured to return False."""

    events: list[dict[str, Any]] = field(default_factory=list)
    return_false: bool = False

    def publish(
        self,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        event_id: str,
        correlation_id: str,
        partition_key: str,
    ) -> bool:
        self.events.append(
            {
                "topic": topic,
                "event_type": event_type,
                "payload": payload,
                "event_id": event_id,
                "correlation_id": correlation_id,
                "partition_key": partition_key,
            }
        )
        return not self.return_false


def make_overlay() -> ModelLabProofJudgeUseOverlay:
    return ModelLabProofJudgeUseOverlay(
        schema_version=1,
        event={
            "topic": USE_TOPIC,
            "event_type": "ModelLabProofJudgeUseRecord",
        },
        ledger={
            "append": {
                "command_env": "EXAMPLE_LEDGER_APPEND_COMMAND",
                "host_env": "EXAMPLE_HOST_NAME",
                "timeout_seconds": 60,
            },
            "lane": "lab-proof-judge",
            "actor": "node:node_lab_proof_judge_use_effect",
            "ticket": "OMN-23642",
            "kind_cell": "lab-proof-class-use",
        },
    )


@pytest.fixture
def overlay() -> ModelLabProofJudgeUseOverlay:
    return make_overlay()


@pytest.fixture
def appender() -> FakeAppender:
    return FakeAppender()


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()
