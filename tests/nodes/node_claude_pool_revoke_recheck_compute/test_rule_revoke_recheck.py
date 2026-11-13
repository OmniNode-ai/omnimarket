# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: the revoke recheck rule, one test per branch and the failing control."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest

from omnimarket.nodes.node_claude_pool_revoke_recheck_compute.handlers.handler_revoke_recheck import (
    HandlerRevokeRecheck,
)
from omnimarket.nodes.node_claude_pool_revoke_recheck_compute.models import (
    ModelLimitProbeAnswer,
    ModelRevokeRecheckPolicy,
    ModelRevokeRecheckRequest,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 11, 21, 0, tzinfo=UTC)
STAMP = datetime(2026, 11, 13, 11, 0, tzinfo=UTC)
REAL_RESET = datetime(2026, 11, 11, 22, 0, tzinfo=UTC)
POLICY = ModelRevokeRecheckPolicy(interval_seconds=3600)


def _request(
    *,
    cause: Literal["revoked", "challenged"] = "revoked",
    revoked_at: datetime | None = STAMP,
    last_probe_at: datetime | None = None,
    policy: ModelRevokeRecheckPolicy | None = POLICY,
    probe: ModelLimitProbeAnswer | None = None,
) -> ModelRevokeRecheckRequest:
    return ModelRevokeRecheckRequest(
        label="alpha",
        fingerprint="4d2430e0",
        cause=cause,
        revoked_at=revoked_at,
        last_probe_at=last_probe_at,
        now=NOW,
        policy=policy,
        probe=probe,
    )


def _answer(
    verdict: Literal["ok", "limited", "refused", "error"],
    reset_at: datetime | None = None,
) -> ModelLimitProbeAnswer:
    return ModelLimitProbeAnswer(verdict=verdict, reset_at=reset_at, probed_at=NOW)


def test_without_the_overlay_block_nothing_is_decided() -> None:
    result = HandlerRevokeRecheck().handle(_request(policy=None))
    assert result.action == "not_configured"
    assert result.command is None


def test_a_label_that_is_not_revoked_is_not_rechecked() -> None:
    result = HandlerRevokeRecheck().handle(_request(revoked_at=None))
    assert result.action == "not_revoked"
    assert result.command is None


def test_a_revoked_label_never_probed_is_due() -> None:
    result = HandlerRevokeRecheck().handle(_request())
    assert result.action == "probe"
    assert result.command is None


def test_a_revoked_label_probed_beyond_the_interval_is_due() -> None:
    last = datetime(2026, 11, 11, 19, 59, tzinfo=UTC)
    assert HandlerRevokeRecheck().handle(_request(last_probe_at=last)).action == "probe"


def test_a_revoked_label_probed_inside_the_interval_waits() -> None:
    last = datetime(2026, 11, 11, 20, 30, tzinfo=UTC)
    assert (
        HandlerRevokeRecheck().handle(_request(last_probe_at=last)).action == "not_due"
    )


def test_an_ok_answer_clears_the_mark() -> None:
    result = HandlerRevokeRecheck().handle(_request(probe=_answer("ok")))
    assert result.action == "clear"
    assert result.command is not None
    assert result.command.action == "clear"
    assert result.command.expected_revoked_at == STAMP
    assert result.command.fingerprint == "4d2430e0"
    assert result.command.new_until is None


def test_a_limited_answer_with_a_reset_after_now_clears_and_carries_new_until() -> None:
    result = HandlerRevokeRecheck().handle(
        _request(probe=_answer("limited", REAL_RESET))
    )
    assert result.action == "clear"
    assert result.command is not None
    assert result.command.action == "clear"
    assert result.command.new_until == REAL_RESET
    assert result.command.expected_revoked_at == STAMP


def test_a_limited_answer_with_no_reset_clears_with_new_until_none() -> None:
    result = HandlerRevokeRecheck().handle(_request(probe=_answer("limited")))
    assert result.action == "clear"
    assert result.command is not None
    assert result.command.action == "clear"
    assert result.command.new_until is None


def test_a_limited_answer_with_a_reset_in_the_past_clears_with_new_until_none() -> None:
    past = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
    result = HandlerRevokeRecheck().handle(_request(probe=_answer("limited", past)))
    assert result.action == "clear"
    assert result.command is not None
    assert result.command.new_until is None


def test_a_refused_answer_on_a_revoked_label_keeps_and_records_the_probe() -> None:
    result = HandlerRevokeRecheck().handle(_request(probe=_answer("refused")))
    assert result.action == "keep"
    assert result.command is not None
    assert result.command.action == "keep"
    assert result.command.expected_revoked_at == STAMP
    assert result.command.probed_at == NOW


def test_a_refused_answer_on_a_challenged_label_confirms() -> None:
    result = HandlerRevokeRecheck().handle(
        _request(cause="challenged", probe=_answer("refused"))
    )
    assert result.action == "confirm"
    assert result.command is not None
    assert result.command.action == "confirm"
    assert result.command.expected_revoked_at == STAMP
    assert result.command.probed_at == NOW
    assert result.command.new_until is None


def test_an_error_answer_keeps_and_records_nothing() -> None:
    result = HandlerRevokeRecheck().handle(_request(probe=_answer("error")))
    assert result.action == "keep"
    assert result.command is None


@pytest.mark.parametrize("cause", ["revoked", "challenged"])
def test_control_refused_never_clears(cause: str) -> None:
    for reset in (None, REAL_RESET, STAMP):
        result = HandlerRevokeRecheck().handle(
            _request(cause=cause, probe=_answer("refused", reset))
        )
        assert result.action != "clear"
        assert result.command is None or result.command.action != "clear"
