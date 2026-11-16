# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool's headless probe reads a limit answer and its reset without carrying a token (OMN-23895).

The call is the injected runner: a fake that returns the typed receipt the real probe module prints. The real call
against a live token is not made here. An answer that is neither a limit nor a clean reply is ``refused``, the
token was tried and failed (OMN-19998), unless its text carries a challenge marker: a 403 challenge page is an
``error`` that proves nothing, never a refusal.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_limit_probe import (
    PROBE_MODULE,
    HeadlessLimitProbe,
    parse_reset,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelLimitProbeSettings,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 11, 23, 0, tzinfo=UTC)
SETTINGS = ModelLimitProbeSettings(
    model="sonnet",
    prompt="Reply with the single word ok",
    timeout_seconds=90,
    limited_patterns=("hit your weekly limit",),
    reset_pattern=r"resets (?P<month>[A-Za-z]{3}) (?P<day>\d{1,2}), (?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<ampm>am|pm)",
    reset_margin_seconds=7,
)
TOKEN = "sk-ant-" + "oat01-" + "x" * 90
#: A 403 challenge page's marker (matched case-insensitively) and an output carrying it.
MARKER = "verify you are human"
CHALLENGE_TEXT = "Access denied: please Verify You Are Human and try again"


def receipt(output: str, cause: str = "answered", code: int | None = 0) -> str:
    return json.dumps(
        {
            "output": output,
            "wall_seconds": 1.5,
            "model": "sonnet",
            "exit_cause": cause,
            "exit_code": code,
        }
    )


class Runner:
    def __init__(self, stdout: str, rc: int = 0) -> None:
        self.stdout, self.rc = stdout, rc
        self.seen: list[tuple[Sequence[str], str, dict[str, str]]] = []

    def __call__(
        self, argv: Sequence[str], stdin: str, env: dict[str, str]
    ) -> tuple[int, str]:
        self.seen.append((argv, stdin, env))
        return self.rc, self.stdout


def probe(
    runner: Runner, *, stored: bool = True, markers: Sequence[str] = ()
) -> HeadlessLimitProbe:
    return HeadlessLimitProbe(
        secret=lambda _label: SecretStr(TOKEN) if stored else None,
        settings=SETTINGS,
        lane_overlay_yaml=lambda: "dependencies: []\n",
        run=runner,
        now=lambda: NOW,
        python="python-under-test",
        challenge_markers=markers,
    )


def test_a_clean_reply_is_ok_and_the_token_only_travels_in_the_environment() -> None:
    runner = Runner(receipt("ok"))
    got = probe(runner).probe("onexbot")
    assert got.verdict == "ok"
    argv, stdin, env = runner.seen[0]
    assert argv[:3] == ["python-under-test", "-m", PROBE_MODULE]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert TOKEN not in " ".join(argv)
    assert TOKEN not in stdin
    assert TOKEN not in got.model_dump_json()


def test_a_limit_answer_is_limited_with_the_reset_it_names_and_the_margin() -> None:
    text = "You've hit your weekly limit · resets Nov 11, 10pm (UTC)"
    got = probe(Runner(receipt(text, code=1))).probe("onexbot")
    assert got.verdict == "limited"
    assert got.reset_at == datetime(2026, 11, 11, 22, 0, 7, tzinfo=UTC)


def test_a_limit_answer_naming_no_reset_is_limited_without_one() -> None:
    got = probe(Runner(receipt("You've hit your weekly limit", code=1))).probe("x")
    assert got.verdict == "limited"
    assert got.reset_at is None


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json",
        receipt("", cause="timeout", code=None),
        receipt("", cause="launch_failed", code=None),
    ],
)
def test_control_anything_else_is_an_error_that_proves_nothing(stdout: str) -> None:
    assert probe(Runner(stdout, rc=1)).probe("onexbot").verdict == "error"


def test_an_answered_failure_with_plain_text_is_refused() -> None:
    text = "Authentication failure: invalid api key"
    got = probe(Runner(receipt(text, code=1))).probe("onexbot")
    assert got.verdict == "refused"
    assert got.probed_at == NOW
    assert got.reset_at is None


def test_a_process_failed_answer_is_refused_without_a_marker() -> None:
    stdout = receipt("something odd", cause="process_failed", code=1)
    assert probe(Runner(stdout)).probe("onexbot").verdict == "refused"


def test_a_challenge_page_is_an_error_that_proves_nothing() -> None:
    runner = Runner(receipt(CHALLENGE_TEXT, code=1))
    got = probe(runner, markers=(MARKER,)).probe("onexbot")
    assert got.verdict == "error"


def test_a_timeout_is_an_error_even_when_the_text_carries_a_marker() -> None:
    runner = Runner(receipt(CHALLENGE_TEXT, cause="timeout", code=None))
    got = probe(runner, markers=(MARKER,)).probe("onexbot")
    assert got.verdict == "error"


@pytest.mark.parametrize("cause", ["answered", "process_failed"])
def test_control_a_challenge_page_is_never_refused(cause: str) -> None:
    runner = Runner(receipt(CHALLENGE_TEXT, cause=cause, code=1))
    got = probe(runner, markers=(MARKER,)).probe("onexbot")
    assert got.verdict != "refused"
    assert got.verdict == "error"


def test_a_label_with_no_stored_token_is_an_error_and_runs_nothing() -> None:
    runner = Runner(receipt("ok"))
    assert probe(runner, stored=False).probe("ghost").verdict == "error"
    assert runner.seen == []


def test_the_token_is_redacted_from_a_receipt_before_it_is_read() -> None:
    runner = Runner(receipt(f"hit your weekly limit {TOKEN}", code=1))
    got = probe(runner).probe("onexbot")
    assert got.verdict == "limited"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("resets Nov 11, 10pm", datetime(2026, 11, 11, 22, 0, 7, tzinfo=UTC)),
        ("resets Nov 13, 11am", datetime(2026, 11, 13, 11, 0, 7, tzinfo=UTC)),
        ("resets Nov 13, 12am", datetime(2026, 11, 13, 0, 0, 7, tzinfo=UTC)),
        ("resets Nov 13, 12:30pm", datetime(2026, 11, 13, 12, 30, 7, tzinfo=UTC)),
        ("resets Jan 2, 3am", datetime(2027, 1, 2, 3, 0, 7, tzinfo=UTC)),
        ("no reset named", None),
        ("resets Foo 13, 11am", None),
        ("resets Feb 30, 11am", None),
    ],
)
def test_parse_reset(text: str, expected: datetime | None) -> None:
    assert parse_reset(text, SETTINGS, NOW) == expected


def test_control_a_hung_probe_process_is_an_error_not_a_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from omnimarket.nodes.node_claude_token_pool_effect.handlers import (
        adapter_limit_probe as module,
    )

    seen: dict[str, object] = {}

    def hung(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd="probe", timeout=1)

    monkeypatch.setattr(module.subprocess, "run", hung)
    got = HeadlessLimitProbe(
        secret=lambda _label: SecretStr(TOKEN),
        settings=SETTINGS,
        lane_overlay_yaml=lambda: "dependencies: []\n",
        now=lambda: NOW,
    ).probe("onexbot")
    assert got.verdict == "error"
    assert seen["timeout"] == SETTINGS.timeout_seconds + 60.0
