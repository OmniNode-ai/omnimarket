# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stale revoked mark clears and a real one stays (OMN-23887).

The staleness compute decides, the pool effect executes the typed clear command, the rotation tick composes. Every
token is fake and built at run time; the pool is the real handler over a temporary store and pool file, the marks
source is a fake returning what the source host's status mirror holds. Failing controls: a revoke with no later
success of the same fingerprint, a different fingerprint, an unconfigured overlay, a changed stamp and an unknown
fingerprint are all kept.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.models.model_claude_pool_revoke_staleness import (
    ModelClearRevokeCommand,
    ModelRevokeObservation,
    ModelRevokeStalenessPolicy,
    ModelRevokeStalenessRequest,
)
from omnimarket.nodes.node_claude_pool_revoke_staleness_compute.handlers.handler_revoke_staleness import (
    HandlerRevokeStaleness,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_rotation_ports import (
    FileTokenFile,
    MirrorMarksSource,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
    fingerprint,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_rotation_tick import (
    HandlerClaudeTokenRotation,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelClaudeTokenRotationRequest,
    ModelRotationMark,
    ModelRotationMarksRead,
    ModelRotationOverlay,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
LABELS = ("acct-a", "caia", "onexbot")
LABEL = "onexbot"
REVOKED = "2026-11-02T07:25:20Z"
LIMITED_AFTER = "2026-11-09T14:07:44Z"
LIMITED_BEFORE = "2026-11-01T00:00:00Z"
STATUS_ROW_KEYS = {
    "label",
    "until",
    "revoked",
    "fingerprint",
    "revoked_at",
    "limited_at",
    "probed_at",
    "revoke_cause",
}


def fake_token(label: str) -> str:
    return "sk-ant-" + "oat01-" + (label.replace("-", "x") * 40)[:96]


class FakeSource:
    def __init__(self, marks: tuple[ModelRotationMark, ...] = ()) -> None:
        self.marks = marks

    def read(self) -> ModelRotationMarksRead:
        return ModelRotationMarksRead(state="read", at=T0, marks=self.marks)


def overlay(margin: int | None = 60) -> ModelRotationOverlay:
    body: dict[str, Any] = {
        "hosts": [
            {
                "name": name,
                "scheduler": "systemd-user",
                "venv": "omnimarket/.venv",
                "token_file": ".config/omni/claude-oauth-token",
            }
            for name in ("h053", "h101")
        ],
        "marks_source": {
            "host": "h053",
            "ssh_target": None,
            "mirror_path": ".cache/omni/claude-token-pool.json",
            "max_age_seconds": 600,
        },
        "events_file": "token-rotations.jsonl",
        "unit_environment": {},
        "schedule": {"interval_seconds": 300},
    }
    if margin is not None:
        body["stale_revoke"] = {"margin_seconds": margin}
    return ModelRotationOverlay.model_validate(body)


class Rig:
    def __init__(
        self,
        tmp: Path,
        host: str,
        *,
        margin: int | None = 60,
        source: FakeSource | None = None,
    ) -> None:
        self.host = host
        self.tmp = tmp / host
        self.pool = HandlerClaudeTokenPool(
            store=LocalByokCredentialStore(db_path=self.tmp / "store" / "e.sqlite"),
            status_path=self.tmp / "cache" / "claude-token-pool.json",
            host=host,
            now=lambda: T0,
        )
        for label in LABELS:
            self.pool.handle(
                ModelClaudeTokenPoolRequest(
                    operation="add",
                    label=label,
                    setup_token_output=SecretStr(fake_token(label)),
                )
            )
        self.tick_handler = HandlerClaudeTokenRotation(
            pool=self.pool,
            token_file=FileTokenFile(self.tmp / "home" / "login"),
            marks_source=source or FakeSource(),
            overlay=overlay(margin),
            host=host,
            events_path=self.tmp / "state" / "events.jsonl",
            now=lambda: T0,
        )

    @property
    def path(self) -> Path:
        return self.tmp / "store" / "claude_token_pool.json"

    def patch(self, label: str, **fields: str | None) -> None:
        body = json.loads(self.path.read_text(encoding="utf-8"))
        for entry in body["tokens"]:
            if entry["label"] == label:
                entry.update(fields)
        self.path.write_text(json.dumps(body), encoding="utf-8")

    def entry(self, label: str) -> dict[str, Any]:
        body = json.loads(self.path.read_text(encoding="utf-8"))
        return next(e for e in body["tokens"] if e["label"] == label)

    def tick(self, *, dry_run: bool = False) -> Any:
        return self.tick_handler.handle(
            ModelClaudeTokenRotationRequest(host=self.host, dry_run=dry_run)
        )


def source_mark(
    fp: str | None = None, limited_at: str | None = LIMITED_AFTER
) -> FakeSource:
    return FakeSource(
        (
            ModelRotationMark(
                label=LABEL,
                revoked=True,
                revoked_at=REVOKED,
                fingerprint=fp or fingerprint(fake_token(LABEL)),
                limited_at=limited_at,
            ),
        )
    )


def test_golden_a_stale_revoke_is_cleared_by_the_tick(tmp_path: Path) -> None:
    rig = Rig(tmp_path, "h101", source=source_mark())
    rig.patch(LABEL, revoked_at=REVOKED)
    result = rig.tick()
    assert result.stale_revoke == "evaluated"
    assert result.revokes_cleared == (LABEL,)
    assert result.revokes_kept == ()
    assert rig.entry(LABEL)["revoked_at"] is None


def test_control_a_real_revoke_with_no_later_success_stays(tmp_path: Path) -> None:
    rig = Rig(tmp_path, "h101", source=source_mark(limited_at=LIMITED_BEFORE))
    rig.patch(LABEL, revoked_at=REVOKED)
    result = rig.tick()
    assert result.stale_revoke == "evaluated"
    assert result.revokes_cleared == ()
    assert result.revokes_kept == (LABEL,)
    assert rig.entry(LABEL)["revoked_at"] == REVOKED


def test_control_a_later_success_of_another_fingerprint_is_no_evidence(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, "h101", source=source_mark(fp="deadbeef"))
    rig.patch(LABEL, revoked_at=REVOKED)
    result = rig.tick()
    assert result.revokes_cleared == ()
    assert rig.entry(LABEL)["revoked_at"] == REVOKED


def test_control_an_old_mirror_without_the_new_keys_keeps_every_revoke(
    tmp_path: Path,
) -> None:
    old = FakeSource((ModelRotationMark(label=LABEL, revoked=True),))
    rig = Rig(tmp_path, "h101", source=old)
    rig.patch(LABEL, revoked_at=REVOKED)
    result = rig.tick()
    assert result.revokes_cleared == ()
    assert result.revokes_kept == (LABEL,)
    assert rig.entry(LABEL)["revoked_at"] == REVOKED


def test_control_an_overlay_without_the_block_changes_nothing(tmp_path: Path) -> None:
    rig = Rig(tmp_path, "h101", margin=None, source=source_mark())
    rig.patch(LABEL, revoked_at=REVOKED)
    result = rig.tick()
    assert result.stale_revoke == "not_configured"
    assert result.revokes_cleared == ()
    assert rig.entry(LABEL)["revoked_at"] == REVOKED


def test_control_a_dry_run_decides_and_writes_nothing(tmp_path: Path) -> None:
    rig = Rig(tmp_path, "h101", source=source_mark())
    rig.patch(LABEL, revoked_at=REVOKED)
    result = rig.tick(dry_run=True)
    assert result.revokes_cleared == (LABEL,)
    assert rig.entry(LABEL)["revoked_at"] == REVOKED


def test_source_host_uses_its_own_later_limit_and_other_hosts_do_not(
    tmp_path: Path,
) -> None:
    source = Rig(tmp_path, "h053")
    source.patch(LABEL, revoked_at=REVOKED, limited_at=LIMITED_AFTER)
    assert source.tick().revokes_cleared == (LABEL,)
    assert source.entry(LABEL)["revoked_at"] is None
    # a limit time a non-source host holds may have been copied by a sync: not evidence there
    other = Rig(tmp_path, "h101", source=FakeSource())
    other.patch(LABEL, revoked_at=REVOKED, limited_at=LIMITED_AFTER)
    result = other.tick()
    assert result.revokes_cleared == ()
    assert other.entry(LABEL)["revoked_at"] == REVOKED


def test_pool_executes_only_a_command_that_matches_its_fingerprint_and_stamp(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, "h101")
    rig.patch(LABEL, revoked_at=REVOKED)
    good = fingerprint(fake_token(LABEL))

    def command(fp: str, stamp: str) -> ModelClearRevokeCommand:
        return ModelClearRevokeCommand(
            label=LABEL,
            fingerprint=fp,
            expected_revoked_at=datetime.fromisoformat(stamp),
            evidence_host="h053",
            evidence_kind="limited",
            evidence_at=datetime.fromisoformat(LIMITED_AFTER),
        )

    def run(cmd: ModelClearRevokeCommand) -> tuple[str, ...]:
        return rig.pool.handle(
            ModelClaudeTokenPoolRequest(
                operation="clear_revokes", clear_commands=(cmd,)
            )
        ).cleared

    # a newer revoke arrived since the decision, and a fingerprint the pool does not hold: both left alone
    assert run(command(good, "2026-11-03T00:00:00+00:00")) == ()
    assert run(command("00000000", "2026-11-02T07:25:20+00:00")) == ()
    assert rig.entry(LABEL)["revoked_at"] == REVOKED
    assert run(command(good, "2026-11-02T07:25:20+00:00")) == (LABEL,)
    assert rig.entry(LABEL)["revoked_at"] is None
    # an already cleared mark is not cleared twice
    assert run(command(good, "2026-11-02T07:25:20+00:00")) == ()


def test_seam_mirror_row_keys_are_written_and_read_by_the_same_names(
    tmp_path: Path,
) -> None:
    probed = "2026-11-10T09:30:00Z"
    rig = Rig(tmp_path, "h053")
    rig.patch(
        LABEL,
        revoked_at=REVOKED,
        limited_at=LIMITED_AFTER,
        revoke_cause="challenged",
        probed_at=probed,
    )
    rig.pool.handle(ModelClaudeTokenPoolRequest(operation="status"))
    mirror = tmp_path / "h053" / "cache" / "claude-token-pool.json"
    rows = json.loads(mirror.read_text(encoding="utf-8"))["tokens"]
    assert all(set(row) == STATUS_ROW_KEYS for row in rows)
    read = MirrorMarksSource(
        home=mirror.parent, mirror_path="claude-token-pool.json", ssh_target=None
    ).read()
    mark = next(m for m in read.marks if m.label == LABEL)
    assert (mark.fingerprint, mark.revoked_at, mark.limited_at) == (
        fingerprint(fake_token(LABEL)),
        REVOKED,
        LIMITED_AFTER,
    )
    assert (mark.revoke_cause, mark.probed_at) == ("challenged", probed)


def test_seam_compute_command_is_accepted_by_the_pool_unchanged(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, "h101")
    rig.patch(LABEL, revoked_at=REVOKED)
    fp = fingerprint(fake_token(LABEL))
    verdict = HandlerRevokeStaleness().handle(
        ModelRevokeStalenessRequest(
            label=LABEL,
            fingerprint=fp,
            revoked_at=datetime(2026, 11, 2, 7, 25, 20, tzinfo=UTC),
            observations=(
                ModelRevokeObservation(
                    host="h053",
                    kind="limited",
                    at=datetime(2026, 11, 9, 14, 7, 44, tzinfo=UTC),
                    fingerprint=fp,
                ),
            ),
            policy=ModelRevokeStalenessPolicy(margin_seconds=60),
        )
    )
    assert verdict.clear is not None
    done = rig.pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="clear_revokes", clear_commands=(verdict.clear,)
        )
    )
    assert done.cleared == (LABEL,)


def test_overlay_tolerates_the_block_being_absent_and_refuses_a_bad_one() -> None:
    assert overlay(margin=None).stale_revoke is None
    assert overlay(margin=60).stale_revoke == ModelRevokeStalenessPolicy(
        margin_seconds=60
    )
    body = overlay(margin=60).model_dump(mode="json")
    body["stale_revoke"] = {"margin_seconds": 60, "surprise": 1}
    with pytest.raises(ValidationError):
        ModelRotationOverlay.model_validate(body)
    body["stale_revoke"] = {"margin_seconds": -1}
    with pytest.raises(ValidationError):
        ModelRotationOverlay.model_validate(body)
