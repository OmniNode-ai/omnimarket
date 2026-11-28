# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude harness runs on the subscription first and on a declared API key second (OMN-26219).

The key is resolved from the secret store by its declared name and reaches only the child's
environment; an unusable login with no usable key is a refusal by name.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_harness_process_runner as runner_module,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_process_runner import (
    HandlerHarnessProcessRunner,
)
from tests.unit.delegation.harness_support_omn26219 import (
    TierSettings,
    load_overlay,
    tier_row,
)

pytestmark = pytest.mark.unit

KEY_VALUE = "sk-ant-not-a-real-value"
KEY_REF = "claude-api-key-ref"


class Environment:
    def __init__(self, path: Path, extra: dict[str, str] | None = None) -> None:
        self._path = path
        self._extra = extra or {}

    def environment(self) -> dict[str, str]:
        return {
            "PATH": f"{self._path}:{os.environ['PATH']}",
            "HOME": str(self._path),
            **self._extra,
        }


def fake_claude(directory: Path, *, login: str) -> Path:
    """A claude whose subscription login is ``limited``, ``absent`` or ``good``; the key always works."""
    seen = directory / "launches.jsonl"
    script = directory / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        "has_key = 'ANTHROPIC_API_KEY' in os.environ\n"
        f"open({str(seen)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'has_key': has_key}}) + '\\n')\n"
        f"login = {login!r}\n"
        "if not has_key and login != 'good':\n"
        "    text = {'limited': 'Claude usage limit reached', 'absent': 'Not logged in - run /login'}[login]\n"
        "    print(json.dumps({'type': 'result', 'result': text, 'is_error': True}))\n"
        "    sys.exit(1)\n"
        "print(json.dumps({'type': 'result', 'result': 'ok', 'is_error': False,\n"
        "  'usage': {'input_tokens': 3, 'output_tokens': 2},\n"
        "  'modelUsage': {'claude-sonnet-5-5': {'inputTokens': 3, 'outputTokens': 2}}}))\n"
    )
    script.chmod(0o700)
    return seen


def launches(seen: Path) -> list[dict[str, Any]]:
    if not seen.exists():
        return []
    return [json.loads(line) for line in seen.read_text().splitlines()]


SONNET = "harness-claude-sonnet"


def agents(*, with_key: bool = True) -> dict[str, Any]:
    return {
        "claude": {"secret_env": {"ANTHROPIC_API_KEY": KEY_REF}} if with_key else {}
    }


def settings(order: list[str] | None) -> TierSettings:
    """The injected settings row of the sonnet tier; ``None`` leaves the tier with no row."""
    return TierSettings({} if order is None else {SONNET: tier_row(order, "medium")})


@pytest.fixture(autouse=True)
def secret_store(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Resolve only the declared reference; record the names asked for, never a value."""
    asked: list[str] = []

    async def resolve(ref: str | None, **_: Any) -> SecretStr | None:
        asked.append(str(ref))
        return SecretStr(KEY_VALUE) if ref == KEY_REF else None

    monkeypatch.setattr(runner_module, "resolve_api_key_async", resolve)
    return asked


def sonnet() -> Any:
    overlay = load_overlay()
    return overlay.backend_by_id()[overlay.tier_by_name()["harness_sonnet"].backend_id]


async def test_declared_api_key_tier_runs_on_the_key(tmp_path: Path) -> None:
    seen = fake_claude(tmp_path, login="good")
    runner = HandlerHarnessProcessRunner(
        environment=Environment(tmp_path),
        agents=agents(),
        settings=settings(["api_key"]),
    )
    got = await runner.run(sonnet(), "p", 20)
    assert got.exit_cause == "completed"
    assert [row["has_key"] for row in launches(seen)] == [True]
    assert KEY_VALUE not in got.model_dump_json()


async def test_subscription_first_and_the_key_is_not_in_that_launch(
    tmp_path: Path,
) -> None:
    seen = fake_claude(tmp_path, login="good")
    runner = HandlerHarnessProcessRunner(
        environment=Environment(tmp_path),
        agents=agents(),
        settings=settings(["subscription", "api_key"]),
    )
    got = await runner.run(sonnet(), "p", 20)
    assert got.exit_cause == "completed"
    assert [row["has_key"] for row in launches(seen)] == [False]


@pytest.mark.parametrize("login", ["limited", "absent"])
async def test_limited_or_absent_login_falls_to_the_key(
    tmp_path: Path, login: str
) -> None:
    seen = fake_claude(tmp_path, login=login)
    runner = HandlerHarnessProcessRunner(
        environment=Environment(tmp_path),
        agents=agents(),
        settings=settings(["subscription", "api_key"]),
    )
    got = await runner.run(sonnet(), "p", 20)
    assert got.exit_cause == "completed"
    assert [row["has_key"] for row in launches(seen)] == [False, True]


@pytest.mark.parametrize("login", ["limited", "absent"])
async def test_no_key_and_no_usable_login_is_a_named_refusal(
    tmp_path: Path, login: str
) -> None:
    seen = fake_claude(tmp_path, login=login)
    runner = HandlerHarnessProcessRunner(
        environment=Environment(tmp_path),
        agents=agents(with_key=False),
        settings=settings(["subscription", "api_key"]),
    )
    got = await runner.run(sonnet(), "p", 20)
    assert got.exit_cause == "not_logged_in"
    assert "secret_env" in got.detail
    assert got.answer == ""
    assert [row["has_key"] for row in launches(seen)] == [False]


async def test_api_key_only_tier_with_no_key_never_launches(tmp_path: Path) -> None:
    seen = fake_claude(tmp_path, login="good")
    runner = HandlerHarnessProcessRunner(
        environment=Environment(tmp_path),
        agents=agents(with_key=False),
        settings=settings(["api_key"]),
    )
    got = await runner.run(sonnet(), "p", 20)
    assert got.exit_cause == "not_logged_in"
    assert "secret_env" in got.detail
    assert launches(seen) == []


async def test_unresolvable_key_is_refused_by_its_declared_name(
    tmp_path: Path,
) -> None:
    seen = fake_claude(tmp_path, login="limited")
    declared = {"claude": {"secret_env": {"ANTHROPIC_API_KEY": "some-other-ref"}}}
    runner = HandlerHarnessProcessRunner(
        environment=Environment(tmp_path),
        agents=declared,
        settings=settings(["subscription", "api_key"]),
    )
    got = await runner.run(sonnet(), "p", 20)
    assert got.exit_cause == "not_logged_in"
    assert "some-other-ref" in got.detail
    assert KEY_VALUE not in got.model_dump_json()
    assert [row["has_key"] for row in launches(seen)] == [False]


async def test_a_subscription_launch_does_not_inherit_a_key(tmp_path: Path) -> None:
    """Kept from the subscription-only rule: a login never bills a key it inherited."""
    seen = fake_claude(tmp_path, login="good")
    runner = HandlerHarnessProcessRunner(
        environment=Environment(tmp_path, {"ANTHROPIC_API_KEY": "sk-ambient"}),
        agents=agents(),
        settings=settings(["subscription"]),
    )
    got = await runner.run(sonnet(), "p", 20)
    assert got.exit_cause == "completed"
    assert [row["has_key"] for row in launches(seen)] == [False]


async def test_a_tier_with_no_setting_is_refused_by_the_row_name(
    tmp_path: Path,
) -> None:
    """No mode is decided by a code default: an absent row is a named refusal and nothing launches."""
    seen = fake_claude(tmp_path, login="good")
    runner = HandlerHarnessProcessRunner(
        environment=Environment(tmp_path), agents=agents(), settings=settings(None)
    )
    got = await runner.run(sonnet(), "p", 20)
    assert got.exit_cause == "credential_refused"
    assert "claude_harness_tiers.tiers.harness-claude-sonnet" in got.detail
    assert launches(seen) == []


async def test_a_runner_with_no_settings_reader_refuses_by_the_row_name(
    tmp_path: Path,
) -> None:
    seen = fake_claude(tmp_path, login="good")
    runner = HandlerHarnessProcessRunner(
        environment=Environment(tmp_path), agents=agents()
    )
    got = await runner.run(sonnet(), "p", 20)
    assert got.exit_cause == "credential_refused"
    assert launches(seen) == []


async def test_effort_comes_from_the_setting(tmp_path: Path) -> None:
    seen = fake_claude(tmp_path, login="good")
    runner = HandlerHarnessProcessRunner(
        environment=Environment(tmp_path),
        agents=agents(),
        settings=TierSettings({SONNET: tier_row(["subscription"], "xhigh")}),
    )
    await runner.run(sonnet(), "p", 20)
    args = launches(seen)[0]["argv"]
    assert args[args.index("--effort") + 1] == "xhigh"


async def test_key_is_never_on_the_command_line(tmp_path: Path) -> None:
    seen = fake_claude(tmp_path, login="good")
    runner = HandlerHarnessProcessRunner(
        environment=Environment(tmp_path),
        agents=agents(),
        settings=settings(["api_key"]),
    )
    await runner.run(sonnet(), "p", 20)
    assert KEY_VALUE not in json.dumps(launches(seen))
