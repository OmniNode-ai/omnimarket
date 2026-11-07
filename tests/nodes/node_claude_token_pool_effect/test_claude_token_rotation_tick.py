# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude token pool rotates a host off a limited or revoked login with no person (OMN-23000).

Every token is fake and assembled at run time. The pool is the real handler over a temporary local
store, the token file is a real file, the marks source is a fake that returns what the source host's
status mirror holds. The failing control plants a limit and shows the host still on the limited
token when no tick runs, and a named injected break (the swap removed) turns the swap assertions red.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_rotation_ports import (
    FileTokenFile,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
    fingerprint,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_rotation import (
    HandlerClaudeTokenRotation,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelClaudeTokenRotationRequest,
    ModelRotationEvent,
    ModelRotationMark,
    ModelRotationMarksRead,
    ModelRotationOverlay,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 7, 14, 0, tzinfo=UTC)
LABELS = ("acct-a", "acct-b", "caia", "onexbot")
HOSTS = ("h053", "h101", "h106", "h201", "h202")


def fake_token(label: str) -> str:
    return "sk-ant-" + "oat01-" + (label.replace("-", "x") * 40)[:96]


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        return self.at


class FakeSource:
    """The one source's marks, as the source host's status mirror reads them."""

    def __init__(self, marks: tuple[ModelRotationMark, ...] = (), *, at: datetime = T0):
        self.marks = marks
        self.at = at
        self.error: str | None = None

    def read(self) -> ModelRotationMarksRead:
        if self.error:
            return ModelRotationMarksRead(state="unreadable", detail=self.error)
        return ModelRotationMarksRead(state="read", at=self.at, marks=self.marks)


def overlay(max_age: int = 600) -> ModelRotationOverlay:
    return ModelRotationOverlay.model_validate(
        {
            "hosts": [
                {
                    "name": name,
                    "scheduler": "systemd-user",
                    "venv": "omnimarket/.venv",
                    "token_file": ".config/omni/claude-oauth-token",
                }
                for name in HOSTS
            ],
            "marks_source": {
                "host": "h053",
                "ssh_target": None,
                "mirror_path": ".cache/omni/claude-token-pool.json",
                "max_age_seconds": max_age,
            },
            "events_file": "token-rotations.jsonl",
            "unit_environment": {},
            "schedule": {"interval_seconds": 300},
        }
    )


class Rig:
    def __init__(self, tmp: Path, host: str = "h101", source: FakeSource | None = None):
        self.clock = Clock()
        self.tmp = tmp / host
        self.pool = HandlerClaudeTokenPool(
            store=LocalByokCredentialStore(db_path=self.tmp / "store" / "e.sqlite"),
            status_path=self.tmp / "cache" / "claude-token-pool.json",
            host=host,
            now=self.clock,
        )
        for label in LABELS:
            self.pool.handle(
                ModelClaudeTokenPoolRequest(
                    operation="add",
                    label=label,
                    setup_token_output=SecretStr(fake_token(label)),
                )
            )
        self.token_path = self.tmp / "home" / ".config" / "omni" / "claude-oauth-token"
        self.token_file = FileTokenFile(self.token_path)
        self.source = source or FakeSource()
        self.events = self.tmp / "state" / "token-rotations.jsonl"
        self.host = host
        self.tick_handler = HandlerClaudeTokenRotation(
            pool=self.pool,
            token_file=self.token_file,
            marks_source=self.source,
            overlay=overlay(),
            host=host,
            events_path=self.events,
            now=self.clock,
        )

    def login(self, label: str) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(fake_token(label) + "\n", encoding="utf-8")
        self.token_path.chmod(0o600)

    def limit(self, label: str, hours: float = 48) -> None:
        self.pool.handle(
            ModelClaudeTokenPoolRequest(
                operation="mark_limited", label=label, seconds=hours * 3600
            )
        )

    def revoke(self, label: str) -> None:
        self.pool.handle(
            ModelClaudeTokenPoolRequest(operation="mark_revoked", label=label)
        )

    def in_use(self) -> str | None:
        got = self.token_path.read_text(encoding="utf-8").strip()
        return next((x for x in LABELS if fake_token(x) == got), None)

    def tick(self):
        return self.tick_handler.handle(ModelClaudeTokenRotationRequest(host=self.host))


def assert_swapped_off(rig: Rig, limited: str) -> None:
    assert rig.in_use() not in (None, limited), f"host still on {rig.in_use()}"


def test_planted_limit_is_swapped_with_no_person(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("caia")
    rig.limit("caia")
    result = rig.tick()
    assert result.outcome == "swapped"
    assert result.from_label == "caia"
    assert result.to_label == rig.in_use()
    assert_swapped_off(rig, "caia")
    assert oct(rig.token_path.stat().st_mode & 0o777) == "0o600"
    # the previous login is kept, for a rollback
    assert rig.token_path.with_name(
        rig.token_path.name + ".prev"
    ).read_text().strip() == fake_token("caia")


def test_failing_control_without_the_tick_the_host_stays_on_the_limited_token(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.login("caia")
    rig.limit("caia")
    # nothing calls the tick: the login stays on the limited account, which is what 11-07 measured
    assert rig.in_use() == "caia"
    with pytest.raises(AssertionError, match="still on caia"):
        assert_swapped_off(rig, "caia")


def test_named_break_swap_removed_turns_the_swap_assertion_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    rig.login("caia")
    rig.limit("caia")
    monkeypatch.setattr(
        HandlerClaudeTokenRotation, "_write_login", lambda *_args, **_kw: None
    )
    rig.tick()
    with pytest.raises(AssertionError, match="still on caia"):
        assert_swapped_off(rig, "caia")


def test_revoked_login_is_swapped(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("onexbot")
    rig.revoke("onexbot")
    result = rig.tick()
    assert result.outcome == "swapped"
    assert_swapped_off(rig, "onexbot")


def test_a_usable_login_is_kept_without_a_write(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("acct-b")
    before = rig.token_path.stat().st_mtime_ns
    result = rig.tick()
    assert result.outcome == "kept"
    assert rig.in_use() == "acct-b"
    assert rig.token_path.stat().st_mtime_ns == before
    assert not rig.events.exists()


def test_a_missing_login_is_filled_from_the_pool(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    result = rig.tick()
    assert result.outcome == "swapped"
    assert result.from_label is None
    assert rig.in_use() is not None


def test_a_login_the_pool_does_not_hold_is_left_alone(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.token_path.parent.mkdir(parents=True)
    rig.token_path.write_text("some-other-account-token\n", encoding="utf-8")
    result = rig.tick()
    assert result.outcome == "unmanaged"
    assert rig.token_path.read_text() == "some-other-account-token\n"


def test_hosts_spread_across_usable_tokens(tmp_path: Path) -> None:
    chosen = []
    for host in HOSTS:
        rig = Rig(tmp_path, host=host)
        rig.login("caia")
        rig.limit("caia")
        assert rig.tick().outcome == "swapped"
        chosen.append(rig.in_use())
    assert "caia" not in chosen
    assert len(set(chosen[:3])) == 3, f"first three hosts drew {chosen[:3]}"
    assert set(chosen) <= {"acct-a", "acct-b", "onexbot"}


def test_every_host_draws_a_different_first_token(tmp_path: Path) -> None:
    firsts = []
    for host in HOSTS[:4]:
        rig = Rig(tmp_path, host=host)
        rig.login("acct-a")
        rig.tick()
        firsts.append(
            rig.pool.handle(ModelClaudeTokenPoolRequest(operation="status"))
            .tokens[0]
            .label
        )
    assert len(set(firsts)) == 4, firsts


def test_source_marks_reach_a_host_that_never_saw_the_limit(tmp_path: Path) -> None:
    until = T0 + timedelta(hours=48)
    source = FakeSource(
        (ModelRotationMark(label="caia", until=until.strftime("%Y-%m-%dT%H:%M:%SZ")),)
    )
    rig = Rig(tmp_path, source=source)
    rig.login("caia")  # no local limit mark: only the source knows
    result = rig.tick()
    assert result.marks_sync.state == "applied"
    assert "caia" in result.marks_sync.changed
    assert result.outcome == "swapped"
    assert_swapped_off(rig, "caia")


def test_a_stale_revoked_mark_is_cleared_from_the_source(tmp_path: Path) -> None:
    source = FakeSource(
        (ModelRotationMark(label="onexbot"),)
    )  # source: not revoked, no limit
    rig = Rig(tmp_path, source=source)
    rig.revoke("onexbot")
    status = rig.pool.handle(ModelClaudeTokenPoolRequest(operation="status"))
    assert status.revoked == ("onexbot",)
    rig.login("onexbot")
    result = rig.tick()
    assert "onexbot" in result.marks_sync.changed
    status = rig.pool.handle(ModelClaudeTokenPoolRequest(operation="status"))
    assert status.revoked == ()
    assert result.outcome == "kept"


def test_an_unreadable_source_leaves_marks_and_still_swaps_by_local_marks(
    tmp_path: Path,
) -> None:
    source = FakeSource()
    source.error = "ssh exit 255"
    rig = Rig(tmp_path, source=source)
    rig.login("caia")
    rig.limit("caia")
    result = rig.tick()
    assert result.marks_sync.state == "unreadable"
    assert result.outcome == "swapped"


def test_a_stale_source_is_refused_and_marks_stay(tmp_path: Path) -> None:
    source = FakeSource(
        (ModelRotationMark(label="onexbot"),), at=T0 - timedelta(hours=3)
    )
    rig = Rig(tmp_path, source=source)
    rig.revoke("onexbot")
    result = rig.tick()
    assert result.marks_sync.state == "stale"
    assert rig.pool.handle(ModelClaudeTokenPoolRequest(operation="status")).revoked == (
        "onexbot",
    )


def test_nothing_usable_alarms_and_keeps_the_login(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("caia")
    for label in LABELS:
        rig.limit(label)
    result = rig.tick()
    assert result.outcome == "no_usable"
    assert rig.in_use() == "caia"
    assert result.alarm is not None
    assert "ALARM" in result.alarm


def test_golden_and_error_chain_events_and_no_token_anywhere(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("caia")
    rig.limit("caia")
    swapped = rig.tick()
    for label in LABELS:
        rig.limit(label)
    rig.login("caia")
    stuck = rig.tick()
    rows = [
        ModelRotationEvent.model_validate_json(line)
        for line in rig.events.read_text().splitlines()
    ]
    assert [r.kind for r in rows] == ["rotated", "no_usable"]
    assert rows[0].host == "h101"
    assert rows[0].from_label == "caia"
    assert rows[0].to_label == swapped.to_label
    assert rows[1].from_label == "caia"
    assert rows[1].to_label is None
    blob = (
        rig.events.read_text()
        + swapped.model_dump_json()
        + stuck.model_dump_json()
        + json.dumps([str(x) for x in (swapped.lines + stuck.lines)])
        + (rig.tmp / "cache" / "claude-token-pool.json").read_text()
    )
    for label in LABELS:
        assert fake_token(label) not in blob
        assert fingerprint(fake_token(label)) not in rig.events.read_text()


def test_sync_marks_on_the_pool_is_authoritative_for_the_labels_it_names(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.limit("acct-a")
    result = rig.pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="sync_marks",
            marks=(
                ModelRotationMark(label="acct-a"),
                ModelRotationMark(label="caia", revoked=True),
            ),
        )
    )
    by = {t.label: t for t in result.tokens}
    assert not by["acct-a"].limited
    assert by["caia"].revoked
    assert not by["acct-b"].limited
    assert not by["acct-b"].revoked
