# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab Claude token effect (OMN-19998): set, add a spare and rotate a limited host,
against a fake host and a fake store. No real token or host is touched; the fake
tokens below are labels, and every test checks none of them leaks into a result,
a script or an argv."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from omnimarket.nodes.node_lab_claude_token_effect.handlers.handler_lab_claude_token import (
    TOKEN_FILE,
    HandlerLabClaudeToken,
    account_ref,
    active_ref,
    limited_ref,
    pool_index_ref,
    pool_ref,
)
from omnimarket.nodes.node_lab_claude_token_effect.models.model_lab_claude_token_request import (
    ModelLabClaudeTokenRequest,
    ModelLabTokenHost,
)
from omnimarket.nodes.node_lab_claude_token_effect.protocols.protocol_lab_token_ports import (
    ShellAnswer,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 16, 22, 30, tzinfo=UTC)
H202 = ModelLabTokenHost(name="h202", target="lab-h202")
FAKE_OLD = "fake-old-token-0000"
FAKE_NEW = "fake-new-token-1111"
FAKE_SPARE_A = "fake-spare-a-2222"
FAKE_SPARE_B = "fake-spare-b-3333"
ALL_FAKES = (FAKE_OLD, FAKE_NEW, FAKE_SPARE_A, FAKE_SPARE_B)


class FakeStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def get(self, ref: str) -> str | None:
        return self.values.get(ref)

    def put(self, ref: str, value: str) -> None:
        self.values[ref] = value

    def delete(self, ref: str) -> None:
        self.values.pop(ref, None)


class FakeHost:
    """One host's token file and its ``.prev`` backup, and how Claude answers each token."""

    def __init__(
        self, current: str | None, answers: dict[str, tuple[int, str]]
    ) -> None:
        self.current = current
        self.prev: str | None = None
        self.answers = answers
        self.calls: list[tuple[str, str, str]] = []
        self.deploy_rc = 0

    def run(
        self, host: ModelLabTokenHost, script: str, *, stdin: str = "", timeout_s: float
    ) -> ShellAnswer:
        op = script.splitlines()[0].removeprefix("# op=")
        self.calls.append((host.name, op, script))
        assert TOKEN_FILE in script
        if op == "deploy":
            if self.deploy_rc:
                return ShellAnswer(self.deploy_rc, "")
            self.prev = self.current
            self.current = stdin
            return ShellAnswer(0, "")
        if op == "restore":
            self.current, self.prev = self.prev, None
            return ShellAnswer(0, "")
        if op == "commit":
            self.prev = None
            return ShellAnswer(0, "")
        if op == "probe":
            rc, out = self.answers.get(self.current or "", (1, "Not logged in"))
            return ShellAnswer(rc, out)
        raise AssertionError(f"unknown op {op}")

    def ops(self) -> list[str]:
        return [op for _, op, _ in self.calls]


def _marker(tmp: Path, host: str, until: datetime) -> Path:
    path = tmp / f".limited-{host}"
    path.write_text(
        json.dumps(
            {
                "host": host,
                "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
        + "\n"
    )
    return path


def _no_leak(result: object, host: FakeHost) -> None:
    text = (
        result.model_dump_json() if hasattr(result, "model_dump_json") else str(result)
    )
    for fake in ALL_FAKES:
        assert fake not in text
        assert fake not in repr(result)
        for _, _, script in host.calls:
            assert fake not in script


def _handler(store: FakeStore, host: FakeHost) -> HandlerLabClaudeToken:
    return HandlerLabClaudeToken(store=store, shell=host, now=lambda: NOW)


# --- set -------------------------------------------------------------------------------------------


def test_set_stores_deploys_probes_and_clears_marker(tmp_path: Path) -> None:
    store = FakeStore()
    host = FakeHost(FAKE_OLD, {FAKE_NEW: (0, "ok\n")})
    marker = _marker(tmp_path, "h202", NOW + timedelta(days=3))
    result = _handler(store, host).handle(
        ModelLabClaudeTokenRequest(
            operation="set",
            host=H202,
            account="acct1",
            token=SecretStr(FAKE_NEW),
            placement_dir=str(tmp_path),
        )
    )
    assert result.outcome == "deployed"
    assert result.probe_answered is True
    assert result.marker_cleared is True
    assert not marker.exists()
    assert host.ops() == ["deploy", "probe", "commit"]
    assert host.current == FAKE_NEW
    assert host.prev is None
    assert store.values[account_ref("h202", "acct1")] == FAKE_NEW
    assert store.values[active_ref("h202")] == "acct1"
    assert result.secret_ref == account_ref("h202", "acct1")
    _no_leak(result, host)


def test_set_deploy_script_writes_mode_600_and_reads_token_from_stdin(
    tmp_path: Path,
) -> None:
    host = FakeHost(None, {FAKE_NEW: (0, "ok")})
    _handler(FakeStore(), host).handle(
        ModelLabClaudeTokenRequest(
            operation="set",
            host=H202,
            account="acct1",
            token=SecretStr(FAKE_NEW),
            placement_dir=str(tmp_path),
        )
    )
    deploy = next(script for _, op, script in host.calls if op == "deploy")
    assert "umask 077" in deploy
    assert "chmod 600" in deploy
    assert 'cat > "$T.new"' in deploy


@pytest.mark.parametrize(
    "answer",
    [
        (0, "You've hit your weekly limit · resets Oct 19, 6pm (America/New_York)"),
        (1, "Invalid API key"),
        (0, "something else"),
    ],
)
def test_set_failed_probe_restores_previous_token_and_store(
    tmp_path: Path, answer: tuple[int, str]
) -> None:
    store = FakeStore(
        {account_ref("h202", "acct1"): FAKE_OLD, active_ref("h202"): "acct1"}
    )
    host = FakeHost(FAKE_OLD, {FAKE_NEW: answer})
    marker = _marker(tmp_path, "h202", NOW + timedelta(days=3))
    result = _handler(store, host).handle(
        ModelLabClaudeTokenRequest(
            operation="set",
            host=H202,
            account="acct1",
            token=SecretStr(FAKE_NEW),
            placement_dir=str(tmp_path),
        )
    )
    assert result.outcome == "restored"
    assert result.probe_answered is False
    assert result.marker_cleared is False
    assert marker.exists()
    assert host.ops() == ["deploy", "probe", "restore"]
    assert host.current == FAKE_OLD
    assert store.values[account_ref("h202", "acct1")] == FAKE_OLD
    assert any("restored" in line for line in result.lines)
    _no_leak(result, host)


def test_set_failed_probe_with_no_previous_token_removes_the_stored_one(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    host = FakeHost(None, {})
    result = _handler(store, host).handle(
        ModelLabClaudeTokenRequest(
            operation="set",
            host=H202,
            account="acct1",
            token=SecretStr(FAKE_NEW),
            placement_dir=str(tmp_path),
        )
    )
    assert result.outcome == "restored"
    assert host.current is None
    assert account_ref("h202", "acct1") not in store.values


def test_set_failed_deploy_never_probes_and_restores_store(tmp_path: Path) -> None:
    store = FakeStore()
    host = FakeHost(FAKE_OLD, {FAKE_NEW: (0, "ok")})
    host.deploy_rc = 255
    result = _handler(store, host).handle(
        ModelLabClaudeTokenRequest(
            operation="set",
            host=H202,
            account="acct1",
            token=SecretStr(FAKE_NEW),
            placement_dir=str(tmp_path),
        )
    )
    assert result.outcome == "restored"
    assert host.ops() == ["deploy", "restore"]
    assert store.values == {}


def test_set_dry_run_prints_references_only_and_touches_nothing(tmp_path: Path) -> None:
    store = FakeStore()
    host = FakeHost(FAKE_OLD, {})
    marker = _marker(tmp_path, "h202", NOW + timedelta(days=3))
    result = _handler(store, host).handle(
        ModelLabClaudeTokenRequest(
            operation="set",
            host=H202,
            account="acct1",
            token=SecretStr(FAKE_NEW),
            placement_dir=str(tmp_path),
            dry_run=True,
        )
    )
    assert result.outcome == "planned"
    assert host.calls == []
    assert store.values == {}
    assert marker.exists()
    text = "\n".join(result.lines)
    assert account_ref("h202", "acct1") in text
    assert TOKEN_FILE in text
    assert "lab-h202" in text
    assert str(marker) in text
    _no_leak(result, host)


def test_token_is_never_an_argument_or_in_repr() -> None:
    request = ModelLabClaudeTokenRequest(
        operation="set",
        host=H202,
        account="acct1",
        token=SecretStr(FAKE_NEW),
        placement_dir="/x",
    )
    assert FAKE_NEW not in repr(request)
    assert FAKE_NEW not in str(request)
    with pytest.raises(ValidationError, match="stdin only"):
        ModelLabClaudeTokenRequest(
            operation="set", host=H202, account="acct1", placement_dir="/x"
        )


# --- add_spare -------------------------------------------------------------------------------------


def test_add_spare_stores_pool_token_and_index_without_deploying(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    host = FakeHost(None, {})
    handler = _handler(store, host)
    for account, token in (
        ("spare-a", FAKE_SPARE_A),
        ("spare-b", FAKE_SPARE_B),
        ("spare-a", FAKE_SPARE_A),
    ):
        result = handler.handle(
            ModelLabClaudeTokenRequest(
                operation="add_spare",
                account=account,
                token=SecretStr(token),
                placement_dir=str(tmp_path),
            )
        )
        assert result.outcome == "spare_added"
        _no_leak(result, host)
    assert host.calls == []
    assert store.values[pool_ref("spare-a")] == FAKE_SPARE_A
    assert store.values[pool_index_ref()] == "spare-a,spare-b"


# --- rotate_limited --------------------------------------------------------------------------------


def _pool_store(**extra: str) -> FakeStore:
    return FakeStore(
        {
            account_ref("h202", "acct1"): FAKE_OLD,
            active_ref("h202"): "acct1",
            pool_ref("spare-a"): FAKE_SPARE_A,
            pool_ref("spare-b"): FAKE_SPARE_B,
            pool_index_ref(): "spare-a,spare-b",
            **extra,
        }
    )


def test_rotate_moves_limited_host_onto_spare_and_records_event(tmp_path: Path) -> None:
    until = NOW + timedelta(days=3)
    marker = _marker(tmp_path, "h202", until)
    store = _pool_store()
    host = FakeHost(FAKE_OLD, {FAKE_SPARE_A: (0, "ok")})
    result = _handler(store, host).handle(
        ModelLabClaudeTokenRequest(
            operation="rotate_limited", host=H202, placement_dir=str(tmp_path)
        )
    )
    assert result.outcome == "rotated"
    assert result.account == "spare-a"
    assert not marker.exists()
    assert result.marker_cleared
    assert host.current == FAKE_SPARE_A
    assert store.values[active_ref("h202")] == "spare-a"
    assert store.values[account_ref("h202", "spare-a")] == FAKE_SPARE_A
    assert store.values[limited_ref("acct1")] == until.strftime("%Y-%m-%dT%H:%M:%SZ")
    (event,) = result.events
    assert (event.host, event.from_account, event.to_account) == (
        "h202",
        "acct1",
        "spare-a",
    )
    assert event.to_secret_ref == account_ref("h202", "spare-a")
    record = (tmp_path / "claude-token-rotations.jsonl").read_text().splitlines()
    assert json.loads(record[-1])["to_account"] == "spare-a"
    _no_leak(result, host)
    for fake in ALL_FAKES:
        assert fake not in "\n".join(record)


def test_rotate_skips_a_spare_that_answers_a_limit_and_takes_the_next(
    tmp_path: Path,
) -> None:
    _marker(tmp_path, "h202", NOW + timedelta(days=3))
    store = _pool_store()
    host = FakeHost(
        FAKE_OLD,
        {FAKE_SPARE_A: (0, "You've hit your weekly limit"), FAKE_SPARE_B: (0, "ok")},
    )
    result = _handler(store, host).handle(
        ModelLabClaudeTokenRequest(
            operation="rotate_limited", host=H202, placement_dir=str(tmp_path)
        )
    )
    assert result.outcome == "rotated"
    assert result.account == "spare-b"
    assert host.ops() == ["deploy", "probe", "restore", "deploy", "probe", "commit"]
    assert limited_ref("spare-a") in store.values
    _no_leak(result, host)


def test_rotate_skips_limited_and_current_accounts(tmp_path: Path) -> None:
    _marker(tmp_path, "h202", NOW + timedelta(days=3))
    store = _pool_store(
        **{
            limited_ref("spare-a"): (NOW + timedelta(hours=2)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            pool_index_ref(): "acct1,spare-a,spare-b",
            pool_ref("acct1"): FAKE_OLD,
        }
    )
    host = FakeHost(FAKE_OLD, {FAKE_SPARE_B: (0, "ok"), FAKE_OLD: (0, "ok")})
    result = _handler(store, host).handle(
        ModelLabClaudeTokenRequest(
            operation="rotate_limited", host=H202, placement_dir=str(tmp_path)
        )
    )
    assert result.account == "spare-b"
    assert host.ops() == ["deploy", "probe", "commit"]


def test_rotate_with_no_spare_says_so_once(tmp_path: Path) -> None:
    marker = _marker(tmp_path, "h202", NOW + timedelta(days=3))
    store = _pool_store()
    host = FakeHost(FAKE_OLD, {})
    handler = _handler(store, host)
    request = ModelLabClaudeTokenRequest(
        operation="rotate_limited", host=H202, placement_dir=str(tmp_path)
    )
    first = handler.handle(request)
    assert first.outcome == "no_spare"
    assert first.reported is True
    assert first.lines
    assert host.current == FAKE_OLD
    assert marker.exists()
    calls = len(host.calls)
    second = handler.handle(request)
    assert second.outcome == "no_spare"
    assert second.reported is False
    assert second.lines == ()
    # Both spares are now marked limited, so the second pass probes nothing.
    assert len(host.calls) == calls
    _no_leak(second, host)


def test_rotate_without_marker_does_nothing(tmp_path: Path) -> None:
    store = _pool_store()
    host = FakeHost(FAKE_OLD, {})
    result = _handler(store, host).handle(
        ModelLabClaudeTokenRequest(
            operation="rotate_limited", host=H202, placement_dir=str(tmp_path)
        )
    )
    assert result.outcome == "not_limited"
    assert host.calls == []


def test_rotate_dry_run_names_candidates_by_reference_only(tmp_path: Path) -> None:
    _marker(tmp_path, "h202", NOW + timedelta(days=3))
    store = _pool_store()
    before = dict(store.values)
    host = FakeHost(FAKE_OLD, {})
    result = _handler(store, host).handle(
        ModelLabClaudeTokenRequest(
            operation="rotate_limited",
            host=H202,
            placement_dir=str(tmp_path),
            dry_run=True,
        )
    )
    assert result.outcome == "planned"
    assert host.calls == []
    assert store.values == before
    text = "\n".join(result.lines)
    assert pool_ref("spare-a") in text
    assert pool_ref("spare-b") in text
    _no_leak(result, host)
