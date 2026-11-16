# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: invalid inputs are refused by validation; an error answer is kept, not cleared."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_claude_pool_revoke_recheck_compute.handlers.handler_revoke_recheck import (
    HandlerRevokeRecheck,
)
from omnimarket.nodes.node_claude_pool_revoke_recheck_compute.models import (
    ModelRevokeRecheckCommand,
    ModelRevokeRecheckRequest,
    ModelRevokeRecheckResult,
)

pytestmark = pytest.mark.unit

REQUEST: dict[str, Any] = {
    "label": "alpha",
    "fingerprint": "4d2430e0",
    "cause": "revoked",
    "revoked_at": "2026-11-13T11:00:00Z",
    "last_probe_at": None,
    "now": "2026-11-11T23:00:00Z",
    "policy": {"interval_seconds": 3600},
    "probe": {"verdict": "error", "probed_at": "2026-11-11T23:00:05Z"},
}


def _refused(request: dict[str, Any], *fragments: str) -> None:
    with pytest.raises(ValidationError) as raised:
        ModelRevokeRecheckRequest.model_validate(request)
    message = str(raised.value)
    for fragment in fragments:
        assert fragment in message


def test_naive_now_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["now"] = "2026-11-11T23:00:00"
    _refused(request, "now", "timezone")


def test_naive_revoked_at_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["revoked_at"] = "2026-11-13T11:00:00"
    _refused(request, "revoked_at", "timezone")


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


def test_unknown_cause_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["cause"] = "expired"
    _refused(request, "cause")


def test_extra_unknown_field_is_refused() -> None:
    request = copy.deepcopy(REQUEST)
    request["unexpected"] = 1
    _refused(request, "unexpected")


def test_new_until_with_a_non_clear_action_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelRevokeRecheckCommand(
            label="alpha",
            fingerprint="4d2430e0",
            expected_revoked_at=datetime(2026, 11, 13, 11, 0, 0, tzinfo=UTC),
            action="confirm",
            new_until=datetime(2026, 11, 11, 23, 30, 0, tzinfo=UTC),
            probed_at=datetime(2026, 11, 11, 23, 0, 5, tzinfo=UTC),
        )


def test_clear_result_without_a_command_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelRevokeRecheckResult(
            label="alpha",
            action="clear",
            reason="the probe answered ok: the revoked mark is cleared",
        )


def test_error_answer_is_kept_with_no_command() -> None:
    result = HandlerRevokeRecheck().handle(
        ModelRevokeRecheckRequest.model_validate(REQUEST)
    )
    assert result.action == "keep"
    assert result.command is None
