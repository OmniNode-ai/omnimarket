# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude token pool rotates per token, not per host, and never shows a token (OMN-17427).

Every token here is fake, assembled at run time so no scanner reads one in the
source. The store is a temporary local secret store, the same class ``onex
secret`` writes.
"""

from __future__ import annotations

import io
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_token_pool_effect import __main__ as cli
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    SECRET_REF_PREFIX,
    ClaudeTokenPoolRefusedError,
    HandlerClaudeTokenPool,
    extract_token,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 18, 2, 0, tzinfo=UTC)


def fake_token(seed: str) -> str:
    return "sk-ant-" + "oat01-" + (seed * 64)[:96]


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        return self.at


Rig = tuple[HandlerClaudeTokenPool, Clock, Path]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    clock = Clock()
    handler = HandlerClaudeTokenPool(
        store=LocalByokCredentialStore(db_path=tmp_path / "store" / "evidence.sqlite"),
        status_path=tmp_path / "cache" / "claude-token-pool.json",
        host="h202",
        now=clock,
    )
    return handler, clock, tmp_path


def add(handler: HandlerClaudeTokenPool, label: str, pasted: str) -> None:
    handler.handle(
        ModelClaudeTokenPoolRequest(
            operation="add", label=label, setup_token_output=SecretStr(pasted)
        )
    )


def op(handler: HandlerClaudeTokenPool, operation: str, **kw: object):
    return handler.handle(ModelClaudeTokenPoolRequest(operation=operation, **kw))


def test_extract_token_from_setup_token_output_wrapped_by_the_terminal() -> None:
    token = fake_token("Ab9_")
    pasted = (
        "\x1b[32m✓ Long-lived authentication token created successfully!\x1b[0m\n\n"
        f"Your OAuth token (valid for 1 year):\n\n{token[:50]}\n{token[50:]}\n\n"
        "Store this token securely. You won't be able to see it again.\n"
    )
    assert extract_token(pasted) == token
    assert extract_token(token + "\n") == token


@pytest.mark.parametrize(
    "pasted",
    ["nothing here", fake_token("a")[:30], f"{fake_token('a')}\n{fake_token('b')}\n"],
)
def test_extract_token_refuses_none_short_or_two(pasted: str) -> None:
    with pytest.raises(ClaudeTokenPoolRefusedError) as refused:
        extract_token(pasted)
    assert "sk-ant-oat01-a" not in str(refused.value)


def test_resolve_rotates_per_token_and_the_host_stays_placeable(rig) -> None:
    handler, clock, _ = rig
    add(handler, "acct-a", fake_token("a"))
    add(handler, "acct-b", fake_token("b"))
    first = op(handler, "resolve")
    assert first.chosen == "acct-a"
    assert first.token is not None
    assert first.token.get_secret_value() == fake_token("a")
    marked = op(handler, "mark_limited", label="acct-a", seconds=3600)
    assert not marked.all_limited
    assert marked.limited == 1
    second = op(handler, "resolve")
    assert second.chosen == "acct-b"
    assert second.token is not None
    assert second.token.get_secret_value() == fake_token("b")
    assert (
        second.status_line
        == "claude-pool host=h202 tokens=2 limited=1 next_reset=2026-10-18T03:00:00Z"
    )
    clock.at = T0 + timedelta(hours=1, seconds=1)
    assert op(handler, "resolve").chosen == "acct-a", (
        "a token whose reset passed is chosen again, first"
    )


def test_all_limited_resolves_nothing_and_raises_the_alarm(rig) -> None:
    handler, _, tmp = rig
    add(handler, "acct-a", fake_token("a"))
    add(handler, "acct-b", fake_token("b"))
    op(handler, "mark_limited", label="acct-a", seconds=7200)
    last = op(handler, "mark_limited", label="acct-b", seconds=3600)
    assert last.all_limited
    assert last.limited == 2
    assert last.next_reset == "2026-10-18T03:00:00Z"
    assert last.status_line.startswith(
        "ALARM claude-pool host=h202 tokens=2 limited=2 "
    )
    assert last.status_line.endswith("every token limited")
    resolved = op(handler, "resolve")
    assert resolved.chosen is None
    assert resolved.token is None
    assert resolved.all_limited
    mirror = json.loads((tmp / "cache" / "claude-token-pool.json").read_text())
    assert mirror["host"] == "h202"
    assert [t["until"] for t in mirror["tokens"]] == [
        "2026-10-18T04:00:00Z",
        "2026-10-18T03:00:00Z",
    ]


def test_a_limit_never_shortens(rig) -> None:
    handler, _, _ = rig
    add(handler, "acct-a", fake_token("a"))
    op(handler, "mark_limited", label="acct-a", seconds=7200)
    assert (
        op(handler, "mark_limited", label="acct-a", seconds=60).next_reset
        == "2026-10-18T04:00:00Z"
    )


def test_empty_pool_status_has_no_alarm(rig) -> None:
    handler, _, _ = rig
    status = op(handler, "status")
    assert (
        status.status_line == "claude-pool host=h202 tokens=0 limited=0 next_reset=none"
    )
    assert not status.all_limited
    assert op(handler, "resolve").token is None


def test_nothing_written_or_returned_carries_a_token(rig) -> None:
    handler, _, tmp = rig
    add(handler, "acct-a", fake_token("a"))
    result = op(handler, "resolve")
    assert fake_token("a") not in result.model_dump_json()
    assert fake_token("a") not in repr(result)
    pool = tmp / "store" / "claude_token_pool.json"
    for path in (pool, tmp / "cache" / "claude-token-pool.json"):
        assert fake_token("a") not in path.read_text()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    import asyncio

    stored = asyncio.run(
        LocalByokCredentialStore(db_path=tmp / "store" / "evidence.sqlite").get_secret(
            SECRET_REF_PREFIX + "acct-a"
        )
    )
    assert stored == fake_token("a")


def test_add_refuses_a_second_label_for_the_same_token_and_a_silent_replace(
    rig,
) -> None:
    handler, _, _ = rig
    add(handler, "acct-a", fake_token("a"))
    with pytest.raises(
        ClaudeTokenPoolRefusedError, match="already in the pool as acct-a"
    ):
        add(handler, "acct-b", fake_token("a"))
    with pytest.raises(ClaudeTokenPoolRefusedError, match="pass --force"):
        add(handler, "acct-a", fake_token("c"))
    with pytest.raises(ClaudeTokenPoolRefusedError, match="lower case"):
        add(handler, "Acct A", fake_token("d"))
    with pytest.raises(ClaudeTokenPoolRefusedError, match="not in this host's pool"):
        op(handler, "mark_limited", label="acct-z", seconds=60)


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_cli_add_and_resolve_never_echo_the_token(rig, monkeypatch, capsys) -> None:
    handler, _, _ = rig
    monkeypatch.setattr("sys.stdin", _Pipe(f"Your OAuth token:\n{fake_token('a')}\n"))
    assert cli.main(["add", "--label", "acct-a"], handler) == 0
    out = capsys.readouterr()
    assert "claude-pool added label=acct-a fingerprint=" in out.out
    assert fake_token("a") not in out.out + out.err
    monkeypatch.setattr("sys.stdout", _Tty())
    assert cli.main(["resolve"], handler) == cli.EXIT_REFUSED
    assert "refusing to write a token to a terminal" in capsys.readouterr().err
    pipe = _Pipe()
    monkeypatch.setattr("sys.stdout", pipe)
    assert cli.main(["resolve"], handler) == 0
    assert pipe.getvalue() == fake_token("a")
    err = capsys.readouterr().err
    assert "label=acct-a" in err
    assert fake_token("a") not in err


def test_cli_exit_codes_for_empty_and_all_limited(rig, monkeypatch, capsys) -> None:
    handler, _, _ = rig
    monkeypatch.setattr("sys.stdout", _Pipe())
    assert cli.main(["resolve"], handler) == cli.EXIT_EMPTY
    monkeypatch.setattr("sys.stdin", _Pipe(fake_token("a")))
    assert cli.main(["add", "--label", "acct-a"], handler) == 0
    assert (
        cli.main(["mark-limited", "--label", "acct-a", "--seconds", "600"], handler)
        == cli.EXIT_ALL_LIMITED
    )
    assert cli.main(["resolve"], handler) == cli.EXIT_ALL_LIMITED
    assert "ALARM claude-pool" in capsys.readouterr().err


def test_forced_replacement_keeps_account_order_and_clears_its_limit(rig) -> None:
    handler, _, _ = rig
    add(handler, "acct-a", fake_token("a"))
    add(handler, "acct-b", fake_token("b"))
    op(handler, "mark_limited", label="acct-a", seconds=3600)
    replacement = op(
        handler,
        "add",
        label="acct-a",
        setup_token_output=SecretStr(fake_token("c")),
        force=True,
    )
    assert [entry.label for entry in replacement.tokens] == ["acct-a", "acct-b"]
    assert replacement.limited == 0
    resolved = op(handler, "resolve")
    assert resolved.chosen == "acct-a"
    assert resolved.token.get_secret_value() == fake_token("c")


def test_mark_requires_reset_seconds_and_preserves_the_pool(rig) -> None:
    handler, _, _ = rig
    add(handler, "acct-a", fake_token("a"))
    with pytest.raises(ClaudeTokenPoolRefusedError, match="needs the seconds"):
        op(handler, "mark_limited", label="acct-a")
    assert op(handler, "status").limited == 0
    assert op(handler, "resolve").chosen == "acct-a"


def test_corrupt_pool_is_refused_without_replacing_it(rig) -> None:
    handler, _, tmp = rig
    path = tmp / "store" / "claude_token_pool.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ClaudeTokenPoolRefusedError, match="is not JSON"):
        op(handler, "status")
    assert path.read_text(encoding="utf-8") == "{not-json"


def test_cli_refuses_bad_input_without_echoing_it(rig, monkeypatch, capsys) -> None:
    handler, _, _ = rig
    pasted = fake_token("a")[:30]
    monkeypatch.setattr("sys.stdin", _Pipe(pasted))
    assert cli.main(["add", "--label", "acct-a"], handler) == cli.EXIT_REFUSED
    output = capsys.readouterr()
    assert "cut short" in output.err
    assert pasted not in output.out + output.err
    assert not op(handler, "status").tokens


def test_cli_json_status_excludes_resolved_token(rig, capsys) -> None:
    handler, _, _ = rig
    add(handler, "acct-a", fake_token("a"))
    op(handler, "resolve")
    assert cli.main(["status", "--json"], handler) == 0
    output = capsys.readouterr()
    status = json.loads(output.out)
    assert status["tokens"][0]["label"] == "acct-a"
    assert "token" not in status
    assert fake_token("a") not in output.out + output.err


def test_terminal_input_uses_a_hidden_prompt(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", _Tty())
    prompts = []

    def hidden(prompt: str) -> str:
        prompts.append(prompt)
        return fake_token("a")

    monkeypatch.setattr(cli, "getpass", hidden)
    assert cli._pasted() == fake_token("a")
    assert len(prompts) == 1
    assert "input hidden" in prompts[0]


def test_a_revoked_token_is_skipped_and_the_good_one_resolves_after_one_failure(
    rig,
) -> None:
    handler, clock, tmp = rig
    add(handler, "h101-own", fake_token("a"))
    add(handler, "acct-1019a", fake_token("b"))
    assert op(handler, "resolve").chosen == "h101-own"
    revoked = op(handler, "mark_revoked", label="h101-own")
    assert not revoked.all_limited, "a good token remains, so the host keeps Claude"
    assert revoked.revoked == ("h101-own",)
    assert revoked.alert is not None
    assert "revoked=h101-own" in revoked.alert
    assert "add --label h101-own --force" in revoked.alert
    resolved = op(handler, "resolve")
    assert resolved.chosen == "acct-1019a"
    assert resolved.token is not None
    assert resolved.token.get_secret_value() == fake_token("b")
    assert resolved.alert is None, "the alert is raised once, by the mark"
    assert resolved.status_line == (
        "claude-pool host=h202 tokens=2 limited=0 next_reset=none revoked=h101-own"
    )
    assert [(t.label, t.revoked) for t in resolved.tokens] == [
        ("h101-own", True),
        ("acct-1019a", False),
    ]
    clock.at = T0 + timedelta(days=60)
    assert op(handler, "resolve").chosen == "acct-1019a", "a revocation never expires"
    mirror = json.loads((tmp / "cache" / "claude-token-pool.json").read_text())
    assert [t["revoked"] for t in mirror["tokens"]] == [True, False]


def test_marking_a_revoked_token_again_raises_no_second_alert(rig) -> None:
    handler, _, _ = rig
    add(handler, "acct-a", fake_token("a"))
    add(handler, "acct-b", fake_token("b"))
    assert op(handler, "mark_revoked", label="acct-a").alert is not None
    again = op(handler, "mark_revoked", label="acct-a")
    assert again.alert is None
    assert again.revoked == ("acct-a",)
    with pytest.raises(ClaudeTokenPoolRefusedError, match="not in this host's pool"):
        op(handler, "mark_revoked", label="acct-z")


def test_every_token_revoked_or_limited_resolves_nothing_and_raises_the_alarm(
    rig,
) -> None:
    handler, _, _ = rig
    add(handler, "acct-a", fake_token("a"))
    add(handler, "acct-b", fake_token("b"))
    op(handler, "mark_limited", label="acct-b", seconds=3600)
    last = op(handler, "mark_revoked", label="acct-a")
    assert last.all_limited
    assert last.status_line == (
        "ALARM claude-pool host=h202 tokens=2 limited=1 "
        "next_reset=2026-10-18T03:00:00Z revoked=acct-a every token limited or revoked"
    )
    resolved = op(handler, "resolve")
    assert resolved.chosen is None
    assert resolved.token is None


def test_forced_re_add_clears_a_revocation(rig) -> None:
    handler, _, _ = rig
    add(handler, "acct-a", fake_token("a"))
    add(handler, "acct-b", fake_token("b"))
    op(handler, "mark_revoked", label="acct-a")
    readded = op(
        handler,
        "add",
        label="acct-a",
        setup_token_output=SecretStr(fake_token("c")),
        force=True,
    )
    assert readded.revoked == ()
    resolved = op(handler, "resolve")
    assert resolved.chosen == "acct-a"
    assert resolved.token.get_secret_value() == fake_token("c")


def test_cli_mark_revoked_exit_codes_and_one_alert_line(
    rig, monkeypatch, capsys
) -> None:
    handler, _, _ = rig
    add(handler, "acct-a", fake_token("a"))
    add(handler, "acct-b", fake_token("b"))
    assert cli.main(["mark-revoked", "--label", "acct-a"], handler) == 0
    out = capsys.readouterr()
    assert out.out.strip() == (
        "claude-pool host=h202 tokens=2 limited=0 next_reset=none revoked=acct-a"
    )
    assert [line for line in out.err.splitlines() if line.startswith("ALERT ")] == [
        out.err.strip()
    ]
    assert "revoked=acct-a" in out.err
    assert cli.main(["mark-revoked", "--label", "acct-a"], handler) == 0
    assert "ALERT" not in capsys.readouterr().err
    assert (
        cli.main(["mark-revoked", "--label", "acct-b"], handler) == cli.EXIT_ALL_LIMITED
    )
    out = capsys.readouterr()
    assert out.out.startswith("ALARM claude-pool")
    assert "revoked=acct-b" in out.err
    monkeypatch.setattr("sys.stdout", _Pipe())
    assert cli.main(["resolve"], handler) == cli.EXIT_ALL_LIMITED
    assert fake_token("a") not in capsys.readouterr().err
