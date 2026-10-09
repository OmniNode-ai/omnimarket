# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node decides and reports what the reconciler it replaces did on the same ledger (OMN-20677).

The cases were captured from the old tool's own code before it was removed: for each
ledger, mode, cap and roster, its stdout, stderr, exit code and the rows it appended.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.ledger_reconcile import ModelReconcileRequest
from tests.nodes.node_ledger_reconcile_effect.fakes import (
    FakeAppender,
    FakeGitHub,
    FakeHost,
)

from .harness import Rig

GOLDEN = Path(__file__).parent / "fixtures/reconcile_legacy_parity.json"
CASES: list[dict[str, Any]] = json.loads(GOLDEN.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[f"case{i}" for i in range(len(CASES))])
def test_the_node_answers_what_the_old_tool_answered(case: dict[str, Any]) -> None:
    rig = Rig(
        FakeHost(case["rows"]),
        github=FakeGitHub({11: "2026-09-22T11:00:00Z"}),
        appender=FakeAppender(error="fixture refusal" if case["append_error"] else ""),
    )
    result = asyncio.run(
        rig.handler.handle(
            ModelReconcileRequest(
                apply=case["apply"],
                max_appends=case["max_appends"],
                live_lanes=frozenset(case["live_lanes"]),
            )
        )
    )
    assert result.exit_code == case["exit_code"]
    assert result.stdout == case["stdout"]
    assert result.stderr == case["stderr"]
    assert rig.appender.rows == case["appends"]
    assert result.commit_sha == ""
