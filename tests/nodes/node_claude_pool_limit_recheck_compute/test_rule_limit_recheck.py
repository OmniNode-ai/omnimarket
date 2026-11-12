# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23887: the limit recheck rule, one test per branch and the failing controls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest

from omnimarket.nodes.node_claude_pool_limit_recheck_compute.handlers.handler_limit_recheck import (
    HandlerLimitRecheck,
)
from omnimarket.nodes.node_claude_pool_limit_recheck_compute.models import (
    ModelLimitProbeAnswer,
    ModelLimitRecheckPolicy,
    ModelLimitRecheckRequest,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 11, 21, 0, tzinfo=UTC)
STAMP = datetime(2026, 11, 13, 11, 0, tzinfo=UTC)
REAL_RESET = datetime(2026, 11, 11, 22, 0, tzinfo=UTC)
POLICY = ModelLimitRecheckPolicy(interval_seconds=3600)


def _request(
    *,
    until: datetime | None = STAMP,
    last_probe_at: datetime | None = None,
    policy: ModelLimitRecheckPolicy | None = POLICY,
    probe: ModelLimitProbeAnswer | None = None,
) -> ModelLimitRecheckRequest:
    return ModelLimitRecheckRequest(
        label="alpha",
        fingerprint="4d2430e0",
        until=until,
        last_probe_at=last_probe_at,
        now=NOW,
        policy=policy,
        probe=probe,
    )


def _answer(
    verdict: Literal["ok", "limited", "error"], reset_at: datetime | None = None
) -> ModelLimitProbeAnswer:
    return ModelLimitProbeAnswer(verdict=verdict, reset_at=reset_at, probed_at=NOW)


def test_without_the_overlay_block_nothing_is_decided() -> None:
    result = HandlerLimitRecheck().handle(_request(policy=None))
    assert result.action == "not_configured"
    assert result.command is None


@pytest.mark.parametrize("until", [None, datetime(2026, 11, 11, 20, 0, tzinfo=UTC)])
def test_a_label_that_is_not_limited_is_not_rechecked(until: datetime | None) -> None:
    result = HandlerLimitRecheck().handle(_request(until=until))
    assert result.action == "not_limited"


def test_a_limited_label_never_probed_is_due() -> None:
    result = HandlerLimitRecheck().handle(_request())
    assert result.action == "probe"
    assert result.command is None


def test_a_limited_label_probed_beyond_the_interval_is_due() -> None:
    last = datetime(2026, 11, 11, 19, 59, tzinfo=UTC)
    assert HandlerLimitRecheck().handle(_request(last_probe_at=last)).action == "probe"


def test_a_limited_label_probed_inside_the_interval_waits() -> None:
    last = datetime(2026, 11, 11, 20, 30, tzinfo=UTC)
    assert (
        HandlerLimitRecheck().handle(_request(last_probe_at=last)).action == "not_due"
    )


def test_an_ok_answer_clears_the_mark() -> None:
    result = HandlerLimitRecheck().handle(_request(probe=_answer("ok")))
    assert result.action == "clear"
    assert result.command is not None
    assert result.command.action == "clear"
    assert result.command.expected_until == STAMP
    assert result.command.fingerprint == "4d2430e0"
    assert result.command.new_until is None


def test_a_newer_earlier_reset_replaces_the_later_stamp() -> None:
    result = HandlerLimitRecheck().handle(
        _request(probe=_answer("limited", REAL_RESET))
    )
    assert result.action == "replace"
    assert result.command is not None
    assert result.command.new_until == REAL_RESET
    assert result.command.expected_until == STAMP


def test_a_limited_answer_with_the_same_reset_keeps_and_records_the_probe() -> None:
    result = HandlerLimitRecheck().handle(_request(probe=_answer("limited", STAMP)))
    assert result.action == "keep"
    assert result.command is not None
    assert result.command.action == "keep"
    assert result.command.probed_at == NOW


def test_a_limited_answer_with_no_reset_keeps_and_records_the_probe() -> None:
    result = HandlerLimitRecheck().handle(_request(probe=_answer("limited")))
    assert result.action == "keep"
    assert result.command is not None


def test_a_limited_answer_with_a_reset_in_the_past_keeps() -> None:
    past = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
    result = HandlerLimitRecheck().handle(_request(probe=_answer("limited", past)))
    assert result.action == "keep"
    assert result.command is not None
    assert result.command.action == "keep"


def test_an_error_answer_keeps_and_records_nothing() -> None:
    result = HandlerLimitRecheck().handle(_request(probe=_answer("error")))
    assert result.action == "keep"
    assert result.command is None


@pytest.mark.parametrize("verdict", ["limited", "error"])
def test_control_ok_answer_is_the_only_thing_that_clears(verdict: str) -> None:
    for reset in (None, REAL_RESET, STAMP):
        result = HandlerLimitRecheck().handle(_request(probe=_answer(verdict, reset)))
        assert result.action != "clear"
        assert result.command is None or result.command.action != "clear"
