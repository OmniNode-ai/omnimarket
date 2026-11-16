# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24651: a GLM call z.ai refuses for an exhausted Coding Plan limit holds GLM out until the reset, in Beijing time.

The failing text is receipt 018b9825-a3b3-4d5d-9338-1b49f4fa47b8 of 2026-11-14: the claude CLI retried the refusal for
188.989 s, and 58 receipts on one host carry the same reset. Which codes hold and the cooldown come from the zai rules
of the quota policy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from omnimarket.handlers.handler_glm_quota_gate import (
    LONGEST_HOLD,
    HandlerGlmQuotaGate,
    exhausted_until,
    plan_limit_rule,
)
from omnimarket.inference.provider_quota_policy import load_provider_quota_policy
from omnimarket.models.delegation.model_glm_quota_gate import (
    ModelGlmQuotaGateRequest,
    ModelGlmQuotaGateResult,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelQuotaCodeRule,
)
from omnimarket.nodes.node_llm_delegation_call_effect.runtime_glm_quota_gate import (
    MARKER_NAME,
    main,
)

RECORDED = (
    "claude-glm exited rc=1: API Error: Request rejected (429) · [1310][Weekly/Monthly Limit Exhausted. "
    "Your limit will reset at 2026-11-15 01:38:18][2026111421065430b8c2c98d0a4d56]"
)
# BUILT_1308 and BUILT_1316 are built texts, not receipts: they carry the shape of the recorded receipt above with the
# code, the sentence and the stamp of the case under test, since no receipt of those codes has been captured.
BUILT_1308 = (
    "claude-glm exited rc=1: API Error: Request rejected (429) · [1308][Usage limit reached for 5 hour. "
    "Your limit will reset at 2026-11-14 23:06:54][2026111421065430b8c2c98d0a4d56]"
)
RATE_LIMIT = "claude-glm exited rc=1: API Error: Request rejected (429) · [1302][Rate limit reached for requests]"
CALL_ENDED = datetime(2026, 11, 14, 13, 6, 54, tzinfo=UTC)
RESET = datetime(2026, 11, 14, 17, 38, 18, tzinfo=UTC)
RESET_1308 = datetime(
    2026, 11, 14, 15, 6, 54, tzinfo=UTC
)  # 23:06:54 Beijing, two hours after the call
FIVE_HOURS = timedelta(hours=5)


def zai_rule(code: str) -> ModelQuotaCodeRule:
    return next(
        r
        for p in load_provider_quota_policy().providers
        if p.provider_id == "zai"
        for r in p.codes
        if r.code == code
    )


def ask(
    operation: Literal["check", "note"],
    now: datetime,
    error: str = "",
    marker: datetime | None = None,
) -> ModelGlmQuotaGateResult:
    return HandlerGlmQuotaGate().handle(
        ModelGlmQuotaGateRequest(
            operation=operation,
            now=now,
            error_text=error,
            marker_until=marker,
        )
    )


def test_the_recorded_reset_is_beijing_time_not_utc() -> None:
    # The request id in the recorded text, 20261114210654, is the call's end (13:06:54Z) plus eight hours.
    noted = exhausted_until(RECORDED, CALL_ENDED)
    assert noted is not None
    assert noted[1] == RESET


def test_a_rate_limit_and_an_unrelated_error_state_no_reset() -> None:
    assert exhausted_until(RATE_LIMIT, CALL_ENDED) is None
    assert exhausted_until("claude-glm exited rc=1: timed out", CALL_ENDED) is None


def test_a_note_of_the_recorded_text_holds_glm_until_the_reset_and_not_after() -> None:
    noted = ask("note", CALL_ENDED, RECORDED)
    assert noted.held
    assert noted.until == RESET
    assert noted.write_until == RESET
    assert noted.code == "1310"
    assert "2026-11-14T17:38:18Z" in noted.reason
    assert ask("check", CALL_ENDED + timedelta(hours=4), marker=RESET).held
    assert not ask("check", RESET + timedelta(seconds=1), marker=RESET).held


def test_a_rate_limit_and_a_check_write_no_hold() -> None:
    assert not ask("note", CALL_ENDED, RATE_LIMIT).held
    assert ask("check", CALL_ENDED, RECORDED).write_until is None


def test_a_limit_notice_with_no_reset_holds_for_the_declared_fallback() -> None:
    fallback = timedelta(seconds=zai_rule("1310").fallback_cooldown_seconds)
    assert fallback > timedelta(0)
    assert (
        ask("note", CALL_ENDED, "API Error: Request rejected (429) · [1310]").until
        == CALL_ENDED + fallback
    )


def test_a_later_marker_is_never_shortened_and_an_absurd_one_is_ignored() -> None:
    later = RESET + timedelta(days=3)
    assert ask("note", CALL_ENDED, RECORDED, marker=later).until == later
    assert not ask("check", CALL_ENDED, marker=datetime(2099, 1, 1, tzinfo=UTC)).held


def test_code_1308_holds_for_its_stated_reset_and_reads_its_rule_from_the_policy() -> (
    None
):
    assert plan_limit_rule(BUILT_1308) is zai_rule("1308") or plan_limit_rule(
        BUILT_1308
    ) == zai_rule("1308")
    noted = ask("note", CALL_ENDED, BUILT_1308)
    assert noted.held is True
    assert noted.code == "1308"
    assert noted.until == RESET_1308
    # A 1308 stating a reset the full five-hour window out holds for the whole window.
    window = (CALL_ENDED + FIVE_HOURS).astimezone(UTC)
    beijing = (window + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    full = ask(
        "note",
        CALL_ENDED,
        f"API Error: Request rejected (429) · [1308][Usage limit reached for 5 hour. Your limit will reset at {beijing}]",
    )
    assert full.until == window


def test_a_1308_that_states_no_reset_holds_for_the_declared_fallback() -> None:
    fallback = timedelta(seconds=zai_rule("1308").fallback_cooldown_seconds)
    held = ask("note", CALL_ENDED, "API Error: Request rejected (429) · [1308]")
    assert held.until == CALL_ENDED + fallback


def test_a_1310_noted_with_its_reset_at_or_before_now_holds_the_unstated_hold() -> None:
    fallback = timedelta(seconds=zai_rule("1310").fallback_cooldown_seconds)
    five_minutes_after_the_reset = RESET + timedelta(minutes=5)
    noted = ask("note", five_minutes_after_the_reset, RECORDED)
    assert noted.held is True
    assert noted.until == five_minutes_after_the_reset + fallback
    # Exactly at the reset is not after now either.
    assert ask("note", RESET, RECORDED).until == RESET + fallback


def test_a_stamp_past_the_longest_hold_is_cut_to_it() -> None:
    far = (CALL_ENDED + timedelta(days=60) + timedelta(hours=8)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    noted = ask(
        "note",
        CALL_ENDED,
        f"API Error: Request rejected (429) · [1310][Weekly/Monthly Limit Exhausted. Your limit will reset at {far}]",
    )
    assert noted.held is True
    assert noted.until == CALL_ENDED + LONGEST_HOLD
    assert noted.write_until == CALL_ENDED + LONGEST_HOLD


@pytest.mark.parametrize(
    "text",
    [
        "the log mentions [1310] and Weekly/Monthly Limit Exhausted but only quotes them",
        "grep '[1310]' glm.log",
        "API Error: Request rejected (500) · [1310][Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-11-15 01:38:18]",
        "Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-11-15 01:38:18",
    ],
)
def test_text_that_only_quotes_a_code_without_the_429_refusal_shape_holds_nothing(
    text: str,
) -> None:
    assert exhausted_until(text, CALL_ENDED) is None
    noted = ask("note", CALL_ENDED, text)
    assert noted.held is False
    assert noted.write_until is None


def run(
    tmp_path: Path, now: datetime, *args: str, capsys: pytest.CaptureFixture[str]
) -> dict[str, object]:
    assert main([args[0], str(tmp_path), *args[1:]], now=now) == 0
    line: dict[str, object] = json.loads(capsys.readouterr().out)
    return line


def test_the_adapter_keeps_the_hold_in_one_marker_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, CALL_ENDED, "check", capsys=capsys)["held"] is False
    assert run(tmp_path, CALL_ENDED, "note", RECORDED, capsys=capsys)["held"] is True
    marker = json.loads((tmp_path / MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["until"] == "2026-11-14T17:38:18Z"
    assert marker["code"] == "1310"
    held = run(tmp_path, CALL_ENDED + timedelta(hours=4), "check", capsys=capsys)
    assert held["held"] is True
    assert "limit is exhausted until 2026-11-14T17:38:18Z" in str(held["reason"])
    assert (
        run(tmp_path, RESET + timedelta(seconds=1), "check", capsys=capsys)["held"]
        is False
    )


def test_the_adapter_writes_the_1308_code_into_the_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, CALL_ENDED, "note", BUILT_1308, capsys=capsys)["held"] is True
    assert (
        json.loads((tmp_path / MARKER_NAME).read_text(encoding="utf-8"))["code"]
        == "1308"
    )


def test_the_adapter_sets_no_marker_for_a_rate_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, CALL_ENDED, "note", RATE_LIMIT, capsys=capsys)["held"] is False
    assert not (tmp_path / MARKER_NAME).exists()


def test_the_adapter_holds_nothing_for_quoted_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    quoted = "see [1310] Weekly/Monthly Limit Exhausted in the docs"
    assert run(tmp_path, CALL_ENDED, "note", quoted, capsys=capsys)["held"] is False
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


@pytest.mark.parametrize("operation", ["check", "note"])
def test_a_state_root_with_a_file_as_a_path_component_prints_not_held_and_exits_zero(
    tmp_path: Path, operation: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """NotADirectoryError from the marker's temp-file cleanup used to escape as a traceback with no JSON line."""
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    state_root = blocker / "state"
    assert main([operation, str(state_root), RECORDED], now=CALL_ENDED) == 0
    line = json.loads(capsys.readouterr().out)
    assert line["held"] is False
    assert line["reason"] == ""
    assert not state_root.exists()


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("policy block missing"),
        AttributeError("'NoneType' object has no attribute 'providers'"),
    ],
    ids=["ValueError", "AttributeError"],
)
def test_an_unreadable_policy_fails_open(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """Any failure of the policy loader prints the JSON line and exits 0, as the module docstring says."""

    def broken() -> None:
        raise failure

    monkeypatch.setattr(
        "omnimarket.handlers.handler_glm_quota_gate.load_provider_quota_policy", broken
    )
    assert main(["note", str(tmp_path), RECORDED], now=CALL_ENDED) == 0
    assert json.loads(capsys.readouterr().out) == {
        "held": False,
        "until": "",
        "reason": "",
    }


# A built text, not a receipt (see BUILT_1308).
BUILT_1316 = (
    "claude-glm exited rc=1: API Error: Request rejected (429) · [1316][Plan window spent. "
    "Your limit will reset at 2026-11-15 01:38:18][2026111421065430b8c2c98d0a4d56]"
)


@pytest.mark.parametrize("code", ["1316", "1317", "1318", "1319", "1320", "1321"])
def test_codes_1316_to_1321_hold_until_the_beijing_time_reset(code: str) -> None:
    text = BUILT_1316.replace("[1316]", f"[{code}]")
    noted = ask("note", CALL_ENDED, text)
    assert noted.held is True
    assert noted.code == code
    assert (
        noted.until == RESET
    )  # 17:38:18Z, not the same digits read as UTC (01:38:18Z the next day)


def test_a_rate_limit_refusal_followed_by_a_plan_limit_refusal_holds_on_the_plan_limit() -> (
    None
):
    text = f"{RATE_LIMIT}\n{RECORDED}"
    rule = plan_limit_rule(text)
    assert rule is not None
    assert rule.code == "1310"
    noted = ask("note", CALL_ENDED, text)
    assert noted.held is True
    assert noted.code == "1310"
    assert noted.until == RESET


STAMPLESS_1308 = "claude-glm exited rc=1: API Error: Request rejected (429) · [1308][Usage limit reached for 5 hour.]"


def test_a_stampless_1308_followed_by_a_1310_holds_until_the_1310_stamp() -> None:
    """The 1308 refusal states no reset; the first "reset at" in the text belongs to the 1310 refusal after it."""
    both = f"{STAMPLESS_1308}\n{RECORDED}"
    noted = ask("note", CALL_ENDED, both)
    assert noted.held is True
    assert noted.code == "1310"
    assert noted.until == RESET
    fallback = timedelta(seconds=zai_rule("1308").fallback_cooldown_seconds)
    assert noted.until > CALL_ENDED + fallback


def test_a_stamp_belongs_to_the_refusal_before_it_not_to_the_one_after() -> None:
    """A stamped 1310 followed by a stampless 1308: the 1308 holds its fallback, the 1310 its stamp; the later hold wins."""
    noted = ask("note", CALL_ENDED, f"{RECORDED}\n{STAMPLESS_1308}")
    assert noted.code == "1310"
    assert noted.until == RESET


def test_each_holding_refusal_is_read_on_its_own_stamp_and_the_latest_hold_wins() -> (
    None
):
    both = f"{BUILT_1308}\n{RECORDED}"
    noted = ask("note", CALL_ENDED, both)
    assert noted.code == "1310"
    assert noted.until == RESET  # 17:38:18Z, after the 1308's 15:06:54Z
    assert ask("note", CALL_ENDED, f"{RECORDED}\n{BUILT_1308}").until == RESET
    assert plan_limit_rule(f"{RATE_LIMIT}\n{RATE_LIMIT}") is None
