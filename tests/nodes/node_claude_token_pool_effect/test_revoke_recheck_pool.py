# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A challenged mark is out of rotation until the revoke recheck clears it (OMN-19998).

The pool stores why a login is out: ``revoked``, a real revoke, or ``challenged``, a 403 challenge page the
pool tick re-tests. A sync of marks carries the source's cause and clears it when the source's revoke ends.
Every token here is fake and built at run time; the pool is the real handler over a
temporary store and pool file with a fixed clock. Failing controls: a command for another fingerprint or a
stale stamp is not executed, and a challenged label is never resolved while another label is usable, nor
when it is the only one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.models.model_claude_pool_revoke_recheck import ModelRevokeRecheckCommand
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationMark,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
T0_STAMP = "2026-11-12T09:00:00Z"
LABELS = ("acct-a", "onexbot")
CHALLENGED_ALERT = (
    "ALERT claude-pool host=h202 challenged=acct-a: a 403 challenge ended a lane "
    "on it; the pool tick re-tests it and clears the mark when it answers"
)


def fake_token(label: str) -> str:
    return "sk-ant-" + "oat01-" + (label.replace("-", "x") * 40)[:96]


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        return self.at


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.host = "h202"
        self.tmp = tmp
        self.clock = Clock()
        self.pool = HandlerClaudeTokenPool(
            store=LocalByokCredentialStore(db_path=self.tmp / "store" / "e.sqlite"),
            status_path=self.tmp / "cache" / "claude-token-pool.json",
            host=self.host,
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

    @property
    def path(self) -> Path:
        return self.tmp / "store" / "claude_token_pool.json"

    @property
    def mirror(self) -> Path:
        return self.tmp / "cache" / "claude-token-pool.json"

    def op(self, operation: str, **kw: Any) -> Any:
        return self.pool.handle(ModelClaudeTokenPoolRequest(operation=operation, **kw))

    def revoke(self, label: str, cause: str = "revoked") -> Any:
        return self.op("mark_revoked", label=label, cause=cause)

    def entry(self, label: str) -> dict[str, Any]:
        body = json.loads(self.path.read_text(encoding="utf-8"))
        return next(e for e in body["tokens"] if e["label"] == label)

    def view(self, label: str) -> Any:
        return next(e for e in self.op("status").tokens if e.label == label)

    def command(self, label: str, **changes: Any) -> ModelRevokeRecheckCommand:
        """The command the recheck compute would send, read off the mark the pool holds."""
        stored = self.entry(label)
        fields: dict[str, Any] = {
            "label": label,
            "fingerprint": stored["fingerprint"],
            "expected_revoked_at": datetime.fromisoformat(stored["revoked_at"]),
            "action": "clear",
            "probed_at": self.clock.at,
        }
        fields.update(changes)
        return ModelRevokeRecheckCommand(**fields)


def test_a_challenge_marks_the_label_out_and_names_it_in_the_line(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    done = rig.revoke("acct-a", cause="challenged")
    assert done.alert == CHALLENGED_ALERT
    assert done.revoked == ("acct-a",), "a challenged label is out of rotation too"
    assert done.challenged == ("acct-a",)
    stored = rig.entry("acct-a")
    assert stored["revoked_at"] == T0_STAMP
    assert stored["revoke_cause"] == "challenged"
    view = rig.view("acct-a")
    assert view.revoked is True
    assert view.revoke_cause == "challenged"
    assert done.status_line == (
        "claude-pool host=h202 tokens=2 limited=0 next_reset=none challenged=acct-a"
    )
    assert rig.op("resolve").chosen == "onexbot", "the challenge keeps the label out"


def test_a_plain_revoke_still_reads_as_it_did(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    done = rig.revoke("acct-a")
    assert done.alert is not None
    assert done.alert.startswith("ALERT claude-pool")
    assert "revoked=acct-a" in done.alert
    assert done.challenged == (), "no cause named means a real revoke, not a challenge"
    assert done.revoked == ("acct-a",)
    assert " revoked=acct-a" in done.status_line
    assert "challenged" not in done.status_line
    assert rig.entry("acct-a")["revoke_cause"] == "revoked"
    assert rig.op("resolve").chosen == "onexbot"


def test_a_later_real_revoke_upgrades_a_challenged_mark(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.revoke("acct-a", cause="challenged")
    marked_at = rig.entry("acct-a")["revoked_at"]
    assert rig.revoke("acct-a", cause="challenged").alert is None, (
        "the same mark raises no second alert"
    )
    rig.clock.at = T0 + timedelta(hours=1)
    again = rig.revoke("acct-a")
    assert again.alert is not None
    assert "revoked=acct-a" in again.alert
    assert "add --label acct-a --force" in again.alert
    stored = rig.entry("acct-a")
    assert stored["revoked_at"] == marked_at, "the upgrade keeps the mark's own time"
    assert stored["revoke_cause"] == "revoked"
    assert again.challenged == ()
    assert again.revoked == ("acct-a",)
    assert "challenged" not in again.status_line


def test_a_clear_command_clears_the_mark_and_the_label_draws_again(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.revoke("acct-a", cause="challenged")
    done = rig.op("apply_revoke_rechecks", revoke_commands=(rig.command("acct-a"),))
    assert done.revoke_rechecks_cleared == ("acct-a",)
    assert done.revoke_rechecks_confirmed == ()
    assert done.revoke_rechecks_kept == ()
    stored = rig.entry("acct-a")
    assert stored["revoked_at"] is None
    assert stored["revoke_cause"] is None
    assert stored["probed_at"] == T0_STAMP
    assert done.revoked == ()
    assert done.challenged == ()
    assert rig.op("resolve").chosen == "acct-a"


def test_a_clear_with_a_new_until_stamps_the_limit_instead(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.revoke("acct-a", cause="challenged")
    done = rig.op(
        "apply_revoke_rechecks",
        revoke_commands=(
            rig.command(
                "acct-a",
                action="clear",
                new_until=datetime(2026, 11, 12, 10, 30, tzinfo=UTC),
            ),
        ),
    )
    assert done.revoke_rechecks_cleared == ("acct-a",)
    stored = rig.entry("acct-a")
    assert stored["revoked_at"] is None
    assert stored["revoke_cause"] is None
    assert stored["until"] == "2026-11-12T10:30:00Z"
    assert stored["limited_at"] == T0_STAMP
    view = rig.view("acct-a")
    assert view.limited is True, "the reset makes the label limited, not revoked"
    assert view.revoked is False
    assert view.until == "2026-11-12T10:30:00Z"
    assert rig.op("resolve").chosen == "onexbot", "the label is limited, not usable"


def test_a_confirm_says_the_revoke_was_real(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.revoke("acct-a", cause="challenged")
    marked_at = rig.entry("acct-a")["revoked_at"]
    done = rig.op(
        "apply_revoke_rechecks",
        revoke_commands=(rig.command("acct-a", action="confirm"),),
    )
    assert done.revoke_rechecks_confirmed == ("acct-a",)
    assert done.revoke_rechecks_cleared == ()
    stored = rig.entry("acct-a")
    assert stored["revoked_at"] == marked_at
    assert stored["revoke_cause"] == "revoked"
    assert stored["probed_at"] == T0_STAMP
    assert done.challenged == ()
    assert rig.op("resolve").chosen == "onexbot"


def test_a_keep_records_only_the_probe_time(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.revoke("acct-a", cause="challenged")
    before = dict(rig.entry("acct-a"))
    done = rig.op(
        "apply_revoke_rechecks",
        revoke_commands=(rig.command("acct-a", action="keep"),),
    )
    assert done.revoke_rechecks_kept == ("acct-a",)
    stored = rig.entry("acct-a")
    assert stored["probed_at"] == T0_STAMP
    for key in ("revoked_at", "revoke_cause", "until", "limited_at"):
        assert stored[key] == before.get(key)
    assert rig.op("resolve").chosen == "onexbot"


def test_a_command_for_another_fingerprint_or_a_stale_stamp_is_skipped(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.revoke("acct-a", cause="challenged")
    stale = datetime.fromisoformat(T0_STAMP) + timedelta(seconds=1)
    for refused in (
        rig.command("acct-a", fingerprint="00000000"),
        rig.command("acct-a", expected_revoked_at=stale),
    ):
        done = rig.op("apply_revoke_rechecks", revoke_commands=(refused,))
        assert done.revoke_rechecks_cleared == ()
        assert done.revoke_rechecks_confirmed == ()
        assert done.revoke_rechecks_kept == ()
    stored = rig.entry("acct-a")
    assert stored["revoked_at"] == T0_STAMP
    assert stored["revoke_cause"] == "challenged"
    assert "probed_at" not in stored, "a skipped command records no probe time"
    done = rig.op("apply_revoke_rechecks", revoke_commands=(rig.command("acct-a"),))
    assert done.revoke_rechecks_cleared == ("acct-a",)


def test_the_status_mirror_carries_the_cause_and_the_probe_time(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.revoke("acct-a", cause="challenged")
    row = next(
        r
        for r in json.loads(rig.mirror.read_text(encoding="utf-8"))["tokens"]
        if r["label"] == "acct-a"
    )
    assert row["revoke_cause"] == "challenged"
    assert row["probed_at"] is None
    assert row["revoked_at"] == T0_STAMP
    rig.op("apply_revoke_rechecks", revoke_commands=(rig.command("acct-a"),))
    row = next(
        r
        for r in json.loads(rig.mirror.read_text(encoding="utf-8"))["tokens"]
        if r["label"] == "acct-a"
    )
    assert row["revoke_cause"] is None
    assert row["probed_at"] == T0_STAMP


def test_sync_marks_carries_a_challenged_cause_and_clears_it_with_the_revoke(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    fields: dict[str, Any] = {
        "label": "acct-a",
        "revoked": True,
        "revoked_at": "2026-11-11T00:00:00Z",
        "fingerprint": rig.entry("acct-a")["fingerprint"],
        "revoke_cause": "challenged",
    }
    rig.op("sync_marks", marks=(ModelRotationMark(**fields),))
    stored = rig.entry("acct-a")
    assert stored["revoked_at"] == "2026-11-11T00:00:00Z"
    assert stored["revoke_cause"] == "challenged"
    rig.op("sync_marks", marks=(ModelRotationMark(label="acct-a", revoked=False),))
    stored = rig.entry("acct-a")
    assert stored["revoked_at"] is None
    assert stored["revoke_cause"] is None, "the cause ends when the revoke ends"


def test_control_a_challenged_label_is_never_resolved(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.revoke("acct-a", cause="challenged")
    first = rig.op("resolve")
    assert first.chosen == "onexbot", (
        "a challenged label is not drawn while another label is usable"
    )
    rig.revoke("onexbot")
    second = rig.op("resolve")
    assert second.chosen is None
    assert second.token is None
    assert second.revoked == ("acct-a", "onexbot")
    assert rig.entry("acct-a")["revoked_at"] == T0_STAMP, (
        "the challenge never dropped the mark that keeps the label out"
    )
