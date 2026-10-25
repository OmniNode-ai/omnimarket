# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Attribution comes from the executor, even when its answer omitted or spoofed it."""

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_execution_attribution_compute import (
    HandlerExecutionAttribution,
)
from omnimarket.nodes.node_execution_attribution_compute.models.model_execution_attribution import (
    ModelExecutionAttributionRequest,
)


@pytest.mark.parametrize(
    "kind", ["merged", "fix_submitted", "blocked", "no_result_file"]
)
@pytest.mark.parametrize(
    ("engine", "model"),
    [
        ("codex_high", "codex"),
        ("claude_opus", "claude-opus"),
        ("glm", "glm"),
        ("code-edit", "code-edit"),
    ],
)
def test_executor_stamps_result_and_terminal(kind, engine, model):
    answer = HandlerExecutionAttribution().handle(
        ModelExecutionAttributionRequest(
            engine=engine,
            host="h101",
            prs=("repo#7",),
            result={"kind": kind, "engine": "wrong", "host": "wrong"},
        )
    )
    assert answer.result == {
        "kind": kind,
        "engine": engine,
        "model": model,
        "host": "h101",
        "pr": "repo#7",
    }
    assert answer.terminal_cells == {
        "engine": engine,
        "model": model,
        "host": "h101",
        "pr": "repo#7",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("engine", "none"),
        ("host", ""),
        ("host", "unknown"),
        ("host", "h101 | engine=opus"),
        ("engine", "codex\nhost=h200"),
    ],
)
def test_missing_or_malformed_executor_is_refused(field, value):
    with pytest.raises(ValidationError):
        ModelExecutionAttributionRequest(
            **{"engine": "codex", "host": "h101", field: value}
        )
