# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool status view names its basis and never reads marks off a stale local copy (OMN-19998).

Every token is fake and assembled at run time. The pool is the real handler over a temporary local
store, the marks source is a fake returning what the source host's status mirror holds, and the
clock is a fixed instant the tests move. The golden case asks a host the overlay does not declare,
whose source reads fresh while the local pool file still holds a revoke the source has cleared, and
shows the view answering from the source and writing nothing; the error cases show the copy-alone
answer naming no limited or revoked label at all; and a named injected break (the old behaviour —
every host trusts its own pool) would name the stale revoke, so the view's own assertion fails
against it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    POOL_FILE_NAME,
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool_status_view import (
    HandlerClaudeTokenPoolStatusView,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool_status_view import (
    ModelPoolStatusViewRequest,
    ModelPoolStatusViewResult,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationMark,
    ModelRotationMarksRead,
    ModelRotationOverlay,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 7, 14, 0, tzinfo=UTC)
LABELS = ("acct-a", "acct-b", "caia", "onexbot")
HOSTS = ("h053", "h101", "h106", "h201", "h202")
#: A host no overlay declares: its answer can only be judged against the marks source.
OFF_OVERLAY_HOST = "h999"


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
        self.calls = 0

    def read(self) -> ModelRotationMarksRead:
        self.calls += 1
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
        self.pool_path = self.tmp / "store" / POOL_FILE_NAME
        self.mirror_path = self.tmp / "cache" / "claude-token-pool.json"
        self.source = source or FakeSource()
        #: What the view's ``local_copy_written_at`` port answers.
        self.copy_written_at: datetime | None = None
        self.host = host

    def view(
        self, overlay_model: ModelRotationOverlay | None = None
    ) -> HandlerClaudeTokenPoolStatusView:
        return HandlerClaudeTokenPoolStatusView(
            pool=self.pool,
            overlay=overlay_model if overlay_model is not None else overlay(),
            marks_source=self.source,
            local_copy_written_at=lambda: self.copy_written_at,
            now=self.clock,
        )

    def view_without_overlay(self) -> HandlerClaudeTokenPoolStatusView:
        return HandlerClaudeTokenPoolStatusView(
            pool=self.pool,
            overlay=None,
            marks_source=self.source,
            local_copy_written_at=lambda: self.copy_written_at,
            now=self.clock,
        )

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

    def local_pool_json(self) -> dict[str, Any]:
        return json.loads(self.pool_path.read_text(encoding="utf-8"))


def old_behaviour_view(rig: Rig) -> ModelPoolStatusViewResult:
    """The named break: the old behaviour — every host trusts its own pool's marks."""
    status = rig.pool.handle(
        ModelClaudeTokenPoolRequest(operation="status", dry_run=True)
    )
    return ModelPoolStatusViewResult(
        host=OFF_OVERLAY_HOST,
        basis="local_pool",
        marks_source_host=None,
        marks_age_seconds=None,
        local_copy_age_seconds=None,
        tokens=len(status.tokens),
        labels=tuple(entry.label for entry in status.tokens),
        limited=tuple(entry.label for entry in status.tokens if entry.limited),
        revoked=status.revoked,
        challenged=status.challenged,
        status_line=status.status_line,
    )


def test_golden_host_off_the_overlay_reads_the_source_and_writes_nothing(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.revoke("onexbot")  # the local copy still holds the stale revoke
    until = (T0 + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rig.source.marks = (
        ModelRotationMark(label="acct-b", until=until),
        ModelRotationMark(label="onexbot"),  # the source: onexbot answers, not revoked
    )
    local = rig.local_pool_json()
    entries = {t["label"]: t for t in local["tokens"]}
    assert entries["onexbot"]["revoked_at"] is not None
    before = rig.pool_path.read_bytes()
    mirror_before = rig.mirror_path.read_bytes()
    result = rig.view().handle(ModelPoolStatusViewRequest(host=OFF_OVERLAY_HOST))
    assert result.basis == "source_marks"
    assert result.marks_source_host == "h053"
    assert result.marks_age_seconds == 0
    revoked = result.revoked
    assert revoked is not None
    assert "onexbot" not in revoked
    limited = result.limited
    assert limited is not None
    assert "acct-b" in limited
    assert result.status_line.endswith(" marks=source:h053 marks_age_s=0"), (
        result.status_line
    )
    # a dry run applies the marks to a view and writes nothing
    assert rig.pool_path.read_bytes() == before
    assert rig.mirror_path.read_bytes() == mirror_before
    assert rig.local_pool_json() == local


def test_error_an_unreadable_source_names_no_marked_label(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.revoke("onexbot")
    rig.limit("acct-a")
    rig.source.error = "ssh exit 255"
    rig.copy_written_at = T0 - timedelta(seconds=30)
    result = rig.view().handle(ModelPoolStatusViewRequest(host=OFF_OVERLAY_HOST))
    assert result.basis == "unsynced_local_copy"
    assert result.limited is None
    assert result.revoked is None
    assert result.challenged is None
    assert result.local_copy_age_seconds == 30
    assert result.detail == "ssh exit 255"
    assert "UNSYNCED" in result.status_line
    assert "age_s=30" in result.status_line
    # the status line never names a label the stale copy marks
    for label in ("onexbot", "acct-a"):
        assert label not in result.status_line
    assert set(result.labels) == set(LABELS)
    assert rig.source.calls == 1


def test_error_a_stale_source_names_the_bound(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        source=FakeSource(
            (ModelRotationMark(label="onexbot"),), at=T0 - timedelta(hours=3)
        ),
    )
    result = rig.view().handle(ModelPoolStatusViewRequest(host=OFF_OVERLAY_HOST))
    assert result.basis == "unsynced_local_copy"
    assert result.revoked is None
    assert result.detail == "source marks are 10800 s old, over the 600 s bound"
    assert "UNSYNCED" in result.status_line
    assert "over the 600 s bound" in result.status_line


def test_error_no_overlay_on_this_host_is_unsynced(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.revoke("onexbot")
    result = rig.view_without_overlay().handle(ModelPoolStatusViewRequest(host="h101"))
    assert result.basis == "unsynced_local_copy"
    assert result.revoked is None
    assert result.limited is None
    assert result.detail == "overlay unreadable on this host"
    assert "age_s=unknown" in result.status_line
    # no overlay, no bound to judge the source against: it is not even asked
    assert rig.source.calls == 0


def test_a_host_in_the_overlay_reads_its_own_pool_and_never_the_source(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.limit("acct-a")
    rig.revoke("onexbot")
    result = rig.view().handle(ModelPoolStatusViewRequest(host="h101"))
    assert result.basis == "local_pool"
    assert rig.source.calls == 0
    limited = result.limited
    assert limited is not None
    assert "acct-a" in limited
    revoked = result.revoked
    assert revoked is not None
    assert "onexbot" in revoked
    assert result.marks_source_host is None
    assert result.marks_age_seconds is None
    own = rig.pool.handle(ModelClaudeTokenPoolRequest(operation="status", dry_run=True))
    assert result.status_line == own.status_line


def test_named_break_the_old_local_pool_answer_names_the_stale_revoke(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.revoke("onexbot")
    rig.source.marks = (ModelRotationMark(label="onexbot"),)
    old = old_behaviour_view(rig)
    # the break, stated: the old behaviour would name onexbot revoked off the stale copy
    assert "onexbot" in (old.revoked or ())
    # so the view's own assertion fails against the old answer
    with pytest.raises(AssertionError, match="onexbot"):
        assert "onexbot" not in (old.revoked or ()), "onexbot is still named revoked"
    # and the view answers from the fresh source instead
    result = rig.view().handle(ModelPoolStatusViewRequest(host=OFF_OVERLAY_HOST))
    assert result.basis == "source_marks"
    revoked = result.revoked
    assert revoked is not None
    assert "onexbot" not in revoked


def test_the_marks_source_host_answers_to_the_address_the_overlay_dials_it_by(
    tmp_path: Path,
) -> None:
    """h053's native hostname is hp-gen9, the overlay's ssh_target: it reads its own pool and never dials itself."""
    rig = Rig(tmp_path, host="hp-gen9")
    body = overlay().model_dump()
    body["marks_source"]["ssh_target"] = "hp-gen9"
    result = rig.view(ModelRotationOverlay.model_validate(body)).handle(
        ModelPoolStatusViewRequest(host="hp-gen9")
    )
    assert result.basis == "local_pool"
    assert rig.source.calls == 0
