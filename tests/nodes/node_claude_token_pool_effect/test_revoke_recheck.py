# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A revoked mark is re-tested by the tick's probe; a token that answers draws again (OMN-19998).

The revoke recheck compute decides, the pool effect executes the typed command, the rotation tick composes and
the probe is the injected port (the limit recheck's). Every token is fake and built at run time; the pool is
the real handler over a temporary store and pool file with a fixed clock. Failing controls: a refused answer
never clears a real revoke (it records the probe time and is re-probed only after the interval), an error
answer records nothing and is probed again on the next tick, a host that does not hold the marks, an overlay
without the block, a dry run and a label with no revoked mark probe nothing, and a refusing pool fails the tick.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.models.model_claude_pool_limit_recheck import ModelLimitProbeAnswer
from omnimarket.models.model_claude_pool_revoke_recheck import (
    ModelRevokeRecheckPolicy,
    ModelRevokeRecheckRequest,
)
from omnimarket.nodes.node_claude_pool_revoke_recheck_compute.handlers.handler_revoke_recheck import (
    HandlerRevokeRecheck,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_rotation_ports import (
    FileTokenFile,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    ClaudeTokenPoolRefusedError,
    HandlerClaudeTokenPool,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_rotation_tick import (
    HandlerClaudeTokenRotation,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelClaudeTokenRotationRequest,
    ModelRotationMarksRead,
    ModelRotationOverlay,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 11, 23, 0, tzinfo=UTC)
NOW_STAMP = "2026-11-11T23:00:00Z"
REVOKED_INSTANT = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
REVOKED_STAMP = "2026-11-11T20:00:00Z"
REAL_RESET = datetime(2026, 11, 12, 4, 0, tzinfo=UTC)
RESET_STAMP = "2026-11-12T04:00:00Z"
LABELS = ("acct-a", "caia", "onexbot")
LABEL = "onexbot"
INTERVAL = 1800
LIMIT_INTERVAL = 3600


def fake_token(label: str) -> str:
    return "sk-ant-" + "oat01-" + (label.replace("-", "x") * 40)[:96]


class FakeProbe:
    """Answers per label; records every label it was asked about."""

    def __init__(self, answers: dict[str, ModelLimitProbeAnswer]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def probe(self, label: str) -> ModelLimitProbeAnswer:
        self.asked.append(label)
        return self.answers[label]


def answer(
    verdict: Literal["ok", "limited", "refused", "error"],
    reset_at: datetime | None = None,
) -> ModelLimitProbeAnswer:
    return ModelLimitProbeAnswer(verdict=verdict, reset_at=reset_at, probed_at=NOW)


class _Source:
    def read(self) -> ModelRotationMarksRead:
        return ModelRotationMarksRead(state="read", at=NOW, marks=())


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def __call__(self) -> datetime:
        return self.at


def overlay(*, revoke: bool = True, source_host: str = "h053") -> ModelRotationOverlay:
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
            "host": source_host,
            "ssh_target": None,
            "mirror_path": ".cache/omni/claude-token-pool.json",
            "max_age_seconds": 600,
        },
        "events_file": "token-rotations.jsonl",
        "unit_environment": {},
        "schedule": {"interval_seconds": 300},
        # the revoke recheck probes with the limit recheck's probe, so its block is always declared
        "limit_recheck": {
            "policy": {"interval_seconds": LIMIT_INTERVAL},
            "probe": {
                "model": "sonnet",
                "prompt": "Reply with the single word ok",
                "timeout_seconds": 90,
                "limited_patterns": ["hit your weekly limit"],
                "reset_pattern": r"resets (?P<month>[A-Za-z]{3}) (?P<day>\d{1,2}), (?P<hour>\d{1,2})(?P<ampm>am|pm)",
                "reset_margin_seconds": 7,
            },
        },
    }
    if revoke:
        body["revoke_recheck"] = {
            "policy": {"interval_seconds": INTERVAL},
            "challenge_markers": ["synthetic-challenge-marker"],
        }
    return ModelRotationOverlay.model_validate(body)


class Rig:
    def __init__(
        self,
        tmp: Path,
        host: str,
        probe: FakeProbe | None,
        *,
        revoke: bool = True,
    ) -> None:
        self.host = host
        self.tmp = tmp / host
        self.clock = Clock()
        self.probe = probe
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
        self.token_file = FileTokenFile(self.tmp / "home" / "login")
        self.handler = HandlerClaudeTokenRotation(
            pool=self.pool,
            token_file=self.token_file,
            marks_source=_Source(),
            overlay=overlay(revoke=revoke),
            host=host,
            events_path=self.tmp / "state" / "events.jsonl",
            now=self.clock,
            limit_probe=probe,
        )

    def revoke(
        self,
        label: str,
        *,
        cause: Literal["revoked", "challenged"] = "revoked",
        at: datetime | None = None,
    ) -> None:
        """Stamp the mark with the pool's own ``mark_revoked``, the clock held back so it is older than now."""
        if at is not None:
            self.clock.at = at
        self.pool.handle(
            ModelClaudeTokenPoolRequest(
                operation="mark_revoked", label=label, cause=cause
            )
        )
        self.clock.at = NOW

    def login(self, label: str) -> None:
        """Hold ``label`` on the host's login file, so the tick finds a usable login."""
        secret = self.pool.secret(label)
        assert secret is not None
        self.token_file.write(secret)

    def entry(self, label: str) -> dict[str, Any]:
        body = json.loads((self.tmp / "store" / "claude_token_pool.json").read_text())
        return next(e for e in body["tokens"] if e["label"] == label)

    def view(self, label: str) -> Any:
        return next(
            e
            for e in self.pool.handle(
                ModelClaudeTokenPoolRequest(operation="status")
            ).tokens
            if e.label == label
        )

    def tick(self, *, dry_run: bool = False) -> Any:
        return self.handler.handle(
            ModelClaudeTokenRotationRequest(host=self.host, dry_run=dry_run)
        )


def test_an_ok_probe_clears_the_revoked_mark_and_the_label_draws_again(
    tmp_path: Path,
) -> None:
    probe = FakeProbe({LABEL: answer("ok")})
    rig = Rig(tmp_path, "h053", probe)
    rig.revoke(LABEL, at=REVOKED_INSTANT)
    rig.login("caia")
    result = rig.tick()
    assert result.revoke_recheck == "evaluated"
    assert result.revokes_rechecked_cleared == (LABEL,)
    assert result.revokes_confirmed == ()
    assert result.revokes_rechecked_kept == ()
    assert result.revoke_rechecks[0].outcome == "cleared"
    assert result.revoke_rechecks[0].probed_at == NOW
    stored = rig.entry(LABEL)
    assert stored["revoked_at"] is None
    assert stored["revoke_cause"] is None
    assert stored["probed_at"] == NOW_STAMP
    assert rig.view(LABEL).revoked is False, "the label is usable again"
    assert result.outcome == "kept"
    assert (
        f"{rig.host}: cleared the revoked mark of {LABEL} after a probe answered"
        in result.lines
    )


def test_a_limited_answer_clears_the_revoke_and_stamps_the_reset(
    tmp_path: Path,
) -> None:
    probe = FakeProbe({LABEL: answer("limited", REAL_RESET)})
    rig = Rig(tmp_path, "h053", probe)
    rig.revoke(LABEL, at=REVOKED_INSTANT)
    result = rig.tick()
    assert result.revokes_rechecked_cleared == (LABEL,)
    stored = rig.entry(LABEL)
    assert stored["revoked_at"] is None
    assert stored["revoke_cause"] is None
    assert stored["until"] == RESET_STAMP
    view = rig.view(LABEL)
    assert view.revoked is False
    assert view.limited is True, "the reset makes the label limited, not revoked"
    assert view.until == RESET_STAMP


def test_control_refused_answer_never_clears(tmp_path: Path) -> None:
    probe = FakeProbe({LABEL: answer("refused")})
    rig = Rig(tmp_path, "h053", probe)
    rig.revoke(LABEL, at=REVOKED_INSTANT)
    result = rig.tick()
    assert result.revoke_recheck == "evaluated"
    assert result.revokes_rechecked_kept == (LABEL,)
    assert result.revokes_rechecked_cleared == ()
    assert result.revoke_rechecks[0].outcome == "kept"
    assert result.revoke_rechecks[0].probed_at == NOW
    stored = rig.entry(LABEL)
    assert stored["revoked_at"] == REVOKED_STAMP
    assert stored["probed_at"] == NOW_STAMP, "the refused probe's time is recorded"
    rig.clock.at = NOW + timedelta(seconds=INTERVAL - 1)
    rig.tick()
    assert probe.asked == [LABEL], "inside the interval the label is not probed again"
    rig.clock.at = NOW + timedelta(seconds=INTERVAL)
    rig.tick()
    assert probe.asked == [LABEL, LABEL], "the interval elapsed: probed again"


def test_an_error_answer_records_no_probe_and_is_asked_again(
    tmp_path: Path,
) -> None:
    probe = FakeProbe({LABEL: answer("error")})
    rig = Rig(tmp_path, "h053", probe)
    rig.revoke(LABEL, at=REVOKED_INSTANT)
    result = rig.tick()
    assert result.revokes_rechecked_kept == (), "an error answer issues no command"
    assert result.revoke_rechecks[0].outcome == "kept"
    stored = rig.entry(LABEL)
    assert stored["revoked_at"] == REVOKED_STAMP
    assert stored.get("probed_at") is None, "an error answer records no probe time"
    rig.tick()
    assert probe.asked == [LABEL, LABEL], "probed again on the next tick"


def test_a_challenged_label_that_refuses_is_confirmed(tmp_path: Path) -> None:
    probe = FakeProbe({LABEL: answer("refused")})
    rig = Rig(tmp_path, "h053", probe)
    rig.revoke(LABEL, cause="challenged", at=REVOKED_INSTANT)
    result = rig.tick()
    assert result.revokes_confirmed == (LABEL,)
    assert result.revokes_rechecked_cleared == ()
    assert result.revoke_rechecks[0].outcome == "confirmed"
    stored = rig.entry(LABEL)
    assert stored["revoke_cause"] == "revoked", "the challenge was a real revoke"
    assert stored["revoked_at"] == REVOKED_STAMP
    assert stored["probed_at"] == NOW_STAMP


def test_control_a_host_that_does_not_hold_the_marks_probes_nothing(
    tmp_path: Path,
) -> None:
    probe = FakeProbe({LABEL: answer("ok")})
    rig = Rig(tmp_path, "h101", probe)
    rig.revoke(LABEL, at=REVOKED_INSTANT)
    result = rig.tick()
    assert result.revoke_recheck == "not_configured"
    assert result.revoke_rechecks == ()
    assert probe.asked == []
    assert rig.entry(LABEL)["revoked_at"] == REVOKED_STAMP


def test_control_an_overlay_without_the_block_probes_nothing(
    tmp_path: Path,
) -> None:
    probe = FakeProbe({LABEL: answer("ok")})
    rig = Rig(tmp_path, "h053", probe, revoke=False)
    rig.revoke(LABEL, at=REVOKED_INSTANT)
    result = rig.tick()
    assert result.revoke_recheck == "not_configured"
    assert probe.asked == []
    assert rig.entry(LABEL)["revoked_at"] == REVOKED_STAMP


def test_control_a_dry_run_probes_nothing_and_reports_due(
    tmp_path: Path,
) -> None:
    probe = FakeProbe({LABEL: answer("ok")})
    rig = Rig(tmp_path, "h053", probe)
    rig.revoke(LABEL, at=REVOKED_INSTANT)
    result = rig.tick(dry_run=True)
    assert result.revoke_recheck == "evaluated"
    assert [o.outcome for o in result.revoke_rechecks] == ["due"]
    assert probe.asked == []
    assert result.revokes_rechecked_cleared == ()
    assert rig.entry(LABEL)["revoked_at"] == REVOKED_STAMP


def test_control_a_label_with_no_revoked_mark_is_never_probed(
    tmp_path: Path,
) -> None:
    probe = FakeProbe({LABEL: answer("ok")})
    rig = Rig(tmp_path, "h053", probe)
    result = rig.tick()
    assert result.revoke_recheck == "evaluated"
    assert result.revoke_rechecks == ()
    assert probe.asked == []


def test_seam_the_compute_command_is_accepted_by_the_pool_unchanged(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, "h053", None)
    rig.revoke(LABEL, at=REVOKED_INSTANT)
    held = rig.view(LABEL)
    verdict = HandlerRevokeRecheck().handle(
        ModelRevokeRecheckRequest(
            label=held.label,
            fingerprint=held.fingerprint,
            cause=held.revoke_cause or "revoked",
            revoked_at=REVOKED_INSTANT,
            last_probe_at=None,
            now=NOW,
            policy=ModelRevokeRecheckPolicy(interval_seconds=INTERVAL),
            probe=answer("ok"),
        )
    )
    assert verdict.command is not None
    done = rig.pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="apply_revoke_rechecks", revoke_commands=(verdict.command,)
        )
    )
    assert done.revoke_rechecks_cleared == (LABEL,)
    assert rig.entry(LABEL)["revoked_at"] is None
    # a newer revoke arrived between the decision and the apply: the stale command is skipped
    rig.revoke(LABEL)
    newer = rig.entry(LABEL)["revoked_at"]
    done = rig.pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="apply_revoke_rechecks", revoke_commands=(verdict.command,)
        )
    )
    assert done.revoke_rechecks_cleared == ()
    assert rig.entry(LABEL)["revoked_at"] == newer


def test_control_a_refusing_pool_propagates_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = FakeProbe({LABEL: answer("ok")})
    rig = Rig(tmp_path, "h053", probe)
    rig.revoke(LABEL, at=REVOKED_INSTANT)
    original = rig.pool.handle

    def refusing(request: ModelClaudeTokenPoolRequest) -> Any:
        if request.operation == "apply_revoke_rechecks":
            raise ClaudeTokenPoolRefusedError("the pool refused the recheck")
        return original(request)

    monkeypatch.setattr(rig.pool, "handle", refusing)
    with pytest.raises(ClaudeTokenPoolRefusedError):
        rig.tick()
    assert rig.entry(LABEL)["revoked_at"] == REVOKED_STAMP, "the mark is not cleared"
