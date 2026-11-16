# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24651: a GLM call z.ai refuses for an exhausted Coding Plan limit holds GLM out until the reset, in Beijing time.

The failing text is receipt 018b9825-a3b3-4d5d-9338-1b49f4fa47b8 of 2026-11-14: the claude CLI retried the refusal for
188.989 s, and 58 receipts on one host carry the same reset.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.handler_glm_quota_gate import (
    MARKER_NAME,
    HandlerGlmQuotaGate,
    exhausted_until,
    main,
)
from omnimarket.models.delegation.model_glm_quota_gate import ModelGlmQuotaGateRequest

RECORDED = (
    "claude-glm exited rc=1: API Error: Request rejected (429) · [1310][Weekly/Monthly Limit Exhausted. "
    "Your limit will reset at 2026-11-15 01:38:18][2026111421065430b8c2c98d0a4d56]"
)
RATE_LIMIT = "claude-glm exited rc=1: API Error: Request rejected (429) · [1302][Rate limit reached for requests]"
CALL_ENDED = datetime(2026, 11, 14, 13, 6, 54, tzinfo=UTC)
RESET = datetime(2026, 11, 14, 17, 38, 18, tzinfo=UTC)


def ask(operation: str, now: datetime, error: str = "", marker: datetime | None = None):
    return HandlerGlmQuotaGate().handle(
        ModelGlmQuotaGateRequest(
            operation=operation, now=now, error_text=error, marker_until=marker
        )
    )


def test_the_recorded_reset_is_beijing_time_not_utc() -> None:
    # The request id in the recorded text, 20261114210654, is the call's end (13:06:54Z) plus eight hours.
    assert exhausted_until(RECORDED, CALL_ENDED) == RESET


def test_a_rate_limit_and_an_unrelated_error_state_no_reset() -> None:
    assert exhausted_until(RATE_LIMIT, CALL_ENDED) is None
    assert exhausted_until("claude-glm exited rc=1: timed out", CALL_ENDED) is None


def test_a_note_of_the_recorded_text_holds_glm_until_the_reset_and_not_after() -> None:
    noted = ask("note", CALL_ENDED, RECORDED)
    assert noted.held
    assert noted.until == RESET
    assert noted.write_until == RESET
    assert "2026-11-14T17:38:18Z" in noted.reason
    assert ask("check", CALL_ENDED + timedelta(hours=4), marker=RESET).held
    assert not ask("check", RESET + timedelta(seconds=1), marker=RESET).held


def test_a_rate_limit_and_a_check_write_no_hold() -> None:
    assert not ask("note", CALL_ENDED, RATE_LIMIT).held
    assert ask("check", CALL_ENDED, RECORDED).write_until is None


def test_a_limit_notice_with_no_reset_holds_for_an_hour() -> None:
    assert ask(
        "note", CALL_ENDED, "API Error: Request rejected (429) · [1310]"
    ).until == CALL_ENDED + timedelta(hours=1)


def test_a_later_marker_is_never_shortened_and_an_absurd_one_is_ignored() -> None:
    later = RESET + timedelta(days=3)
    assert ask("note", CALL_ENDED, RECORDED, marker=later).until == later
    assert not ask("check", CALL_ENDED, marker=datetime(2099, 1, 1, tzinfo=UTC)).held


def run(
    tmp_path: Path, now: datetime, *args: str, capsys: pytest.CaptureFixture[str]
) -> dict[str, object]:
    assert main([args[0], str(tmp_path), *args[1:]], now=now) == 0
    return json.loads(capsys.readouterr().out)


def test_the_adapter_keeps_the_hold_in_one_marker_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, CALL_ENDED, "check", capsys=capsys)["held"] is False
    assert run(tmp_path, CALL_ENDED, "note", RECORDED, capsys=capsys)["held"] is True
    assert (
        json.loads((tmp_path / MARKER_NAME).read_text(encoding="utf-8"))["until"]
        == "2026-11-14T17:38:18Z"
    )
    held = run(tmp_path, CALL_ENDED + timedelta(hours=4), "check", capsys=capsys)
    assert held["held"] is True
    assert "limit is exhausted until 2026-11-14T17:38:18Z" in str(held["reason"])
    assert (
        run(tmp_path, RESET + timedelta(seconds=1), "check", capsys=capsys)["held"]
        is False
    )


def test_the_adapter_sets_no_marker_for_a_rate_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, CALL_ENDED, "note", RATE_LIMIT, capsys=capsys)["held"] is False
    assert not (tmp_path / MARKER_NAME).exists()


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"until": "tomorrow"}', '{"until": "2026-11-14T17:38:18"}'],
)
def test_an_unreadable_marker_reads_as_not_held(
    tmp_path: Path, content: str, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / MARKER_NAME).write_text(content, encoding="utf-8")
    assert run(tmp_path, CALL_ENDED, "check", capsys=capsys)["held"] is False


def test_a_malformed_call_fails_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["nonsense"], now=CALL_ENDED) == 2
    assert json.loads(capsys.readouterr().out)["held"] is False
