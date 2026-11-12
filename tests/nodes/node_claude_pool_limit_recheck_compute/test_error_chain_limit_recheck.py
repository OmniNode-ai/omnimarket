# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23887: invalid inputs are refused by validation; an error answer is kept, not cleared."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_claude_pool_limit_recheck_compute.handlers.handler_limit_recheck import (
    HandlerLimitRecheck,
)
from omnimarket.nodes.node_claude_pool_limit_recheck_compute.models import (
    ModelLimitRecheckCommand,
    ModelLimitRecheckRequest,
)

pytestmark = pytest.mark.unit

REQUEST: dict[str, Any] = {
    "label": "alpha",
    "fingerprint": "4d2430e0",
    "until": "2026-11-13T11:00:00Z",
    "last_probe_at": None,
    "now": "2026-11-11T23:00:00Z",
    "policy": {"interval_seconds": 3600},
    "probe": {"verdict": "error", "probed_at": "2026-11-11T23:00:05Z"},
}


def _refused(request: dict[str, Any], *fragments: str) -> None:
    with pytest.raises(ValidationError) as raised:
        ModelLimitRecheckRequest.model_validate(request)
    message = str(raised.value)
    for fragment in fragments:
        assert fragment in message


def test_naive_now_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["now"] = "2026-11-11T23:00:00"
    _refused(request, "now", "timezone")


def test_naive_until_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["until"] = "2026-11-13T11:00:00"
    _refused(request, "until", "timezone")


def test_naive_probe_time_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["probe"]["probed_at"] = "2026-11-11T23:00:05"
    _refused(request, "probed_at", "timezone")


def test_interval_under_a_minute_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["policy"]["interval_seconds"] = 30
    _refused(request, "interval_seconds")


def test_unknown_verdict_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["probe"]["verdict"] = "fine"
    _refused(request, "verdict")


def test_extra_unknown_field_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["unexpected"] = 1
    _refused(request, "unexpected")


def test_replace_command_without_new_until_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLimitRecheckCommand(
            label="alpha",
            fingerprint="4d2430e0",
            expected_until="2026-11-13T11:00:00Z",  # type: ignore[arg-type]
            action="replace",
            probed_at="2026-11-11T23:00:05Z",  # type: ignore[arg-type]
        )


def test_error_answer_is_kept_with_no_command() -> None:
    result = HandlerLimitRecheck().handle(
        ModelLimitRecheckRequest.model_validate(REQUEST)
    )
    assert result.action == "keep"
    assert result.command is None
