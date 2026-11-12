# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stale limited mark clears on an ok probe and a real limit stays (OMN-23895).

The limit recheck compute decides, the pool effect executes the typed command, the rotation tick composes and the
probe is the injected port. Every token is fake and built at run time; the pool is the real handler over a temporary
store and pool file. Failing controls: a limited or error answer never clears, a command for another fingerprint or a
changed stamp is not executed, a host that does not hold the marks probes nothing, an overlay without the block
probes nothing, and a label inside the probe interval is not probed again.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_pool_limit_recheck_compute.models.model_limit_recheck import (
    ModelLimitProbeAnswer,
    ModelLimitRecheckCommand,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_rotation_ports import (
    FileTokenFile,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    ClaudeTokenPoolRefusedError,
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
    ModelRotationMarksRead,
    ModelRotationOverlay,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 11, 23, 0, tzinfo=UTC)
STALE_STAMP = "2026-11-13T11:00:00Z"
REAL_RESET = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)
LABELS = ("acct-a", "caia", "onexbot")
INTERVAL = 3600


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


def answer(verdict: str, reset_at: datetime | None = None) -> ModelLimitProbeAnswer:
    return ModelLimitProbeAnswer(verdict=verdict, reset_at=reset_at, probed_at=NOW)  # type: ignore[arg-type]


class _Source:
    def read(self) -> ModelRotationMarksRead:
        return ModelRotationMarksRead(state="read", at=NOW, marks=())


def overlay(*, recheck: bool = True, source_host: str = "h053") -> ModelRotationOverlay:
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
    }
    if recheck:
        body["limit_recheck"] = {
            "policy": {"interval_seconds": INTERVAL},
            "probe": {
                "model": "sonnet",
                "prompt": "Reply with the single word ok",
                "timeout_seconds": 90,
                "limited_patterns": ["hit your weekly limit"],
                "reset_pattern": r"resets (?P<month>[A-Za-z]{3}) (?P<day>\d{1,2}), (?P<hour>\d{1,2})(?P<ampm>am|pm)",
                "reset_margin_seconds": 7,
            },
        }
    return ModelRotationOverlay.model_validate(body)


class Rig:
    def __init__(
        self,
        tmp: Path,
        host: str,
        probe: FakeProbe | None,
        *,
        recheck: bool = True,
        clock: datetime = NOW,
    ) -> None:
        self.host = host
        self.tmp = tmp / host
        self.now = clock
        self.pool = HandlerClaudeTokenPool(
            store=LocalByokCredentialStore(db_path=self.tmp / "store" / "e.sqlite"),
            status_path=self.tmp / "cache" / "claude-token-pool.json",
            host=host,
            now=lambda: self.now,
        )
        for label in LABELS:
            self.pool.handle(
                ModelClaudeTokenPoolRequest(
                    operation="add",
                    label=label,
                    setup_token_output=SecretStr(fake_token(label)),
                )
            )
        self.probe = probe
        self.handler = HandlerClaudeTokenRotation(
            pool=self.pool,
            token_file=FileTokenFile(self.tmp / "home" / "login"),
            marks_source=_Source(),
            overlay=overlay(recheck=recheck),
            host=host,
            events_path=self.tmp / "state" / "events.jsonl",
            now=lambda: self.now,
            limit_probe=probe,
        )

    def limit(self, label: str, until: str) -> None:
        seconds = (
            datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            - self.now
        ).total_seconds()
        self.pool.handle(
            ModelClaudeTokenPoolRequest(
                operation="mark_limited", label=label, seconds=seconds
            )
        )

    def entry(self, label: str) -> Any:
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


def test_an_ok_probe_clears_the_stale_mark_and_a_real_limit_stays(
    tmp_path: Path,
) -> None:
    probe = FakeProbe(
        {
            "onexbot": answer("ok"),
            "caia": answer("limited", REAL_RESET),
        }
    )
    rig = Rig(tmp_path, "h053", probe)
    rig.limit("onexbot", STALE_STAMP)
    rig.limit("caia", "2026-11-14T04:00:00Z")
    result = rig.tick()
    assert result.limit_recheck == "evaluated"
    assert result.limits_cleared == ("onexbot",)
    assert not rig.entry("onexbot").limited
    assert rig.entry("onexbot").until is None
    # caia answered limited with the reset it already carries: the mark stays, the probe time is recorded
    assert rig.entry("caia").limited
    assert rig.entry("caia").until == "2026-11-14T04:00:00Z"
    assert rig.entry("caia").probed_at == "2026-11-11T23:00:00Z"
    assert result.limits_kept == ("caia",)
    assert sorted(probe.asked) == ["caia", "onexbot"]


def test_a_newer_earlier_reset_replaces_the_stamp(tmp_path: Path) -> None:
    earlier = datetime(2026, 11, 12, 22, 0, 7, tzinfo=UTC)
    rig = Rig(tmp_path, "h053", FakeProbe({"onexbot": answer("limited", earlier)}))
    rig.limit("onexbot", STALE_STAMP)
    result = rig.tick()
    assert result.limits_replaced == ("onexbot",)
    assert rig.entry("onexbot").until == "2026-11-12T22:00:07Z"
    assert rig.entry("onexbot").limited


@pytest.mark.parametrize("verdict", ["limited", "error"])
def test_control_only_an_ok_answer_clears(tmp_path: Path, verdict: str) -> None:
    rig = Rig(tmp_path, "h053", FakeProbe({"onexbot": answer(verdict)}))
    rig.limit("onexbot", STALE_STAMP)
    result = rig.tick()
    assert result.limits_cleared == ()
    assert rig.entry("onexbot").limited
    assert rig.entry("onexbot").until == STALE_STAMP


def test_an_error_answer_records_no_probe_so_the_label_is_asked_again(
    tmp_path: Path,
) -> None:
    probe = FakeProbe({"onexbot": answer("error")})
    rig = Rig(tmp_path, "h053", probe)
    rig.limit("onexbot", STALE_STAMP)
    rig.tick()
    rig.tick()
    assert probe.asked == ["onexbot", "onexbot"]
    assert rig.entry("onexbot").probed_at is None


def test_a_label_probed_inside_the_interval_is_not_probed_again(
    tmp_path: Path,
) -> None:
    probe = FakeProbe({"onexbot": answer("limited")})
    rig = Rig(tmp_path, "h053", probe)
    rig.limit("onexbot", STALE_STAMP)
    rig.tick()
    rig.now = NOW + timedelta(seconds=INTERVAL - 1)
    rig.tick()
    assert probe.asked == ["onexbot"]
    rig.now = NOW + timedelta(seconds=INTERVAL)
    rig.tick()
    assert probe.asked == ["onexbot", "onexbot"]


def test_a_host_that_does_not_hold_the_marks_probes_nothing(tmp_path: Path) -> None:
    probe = FakeProbe({"onexbot": answer("ok")})
    rig = Rig(tmp_path, "h101", probe)
    rig.limit("onexbot", STALE_STAMP)
    result = rig.tick()
    assert result.limit_recheck == "not_configured"
    assert probe.asked == []
    assert rig.entry("onexbot").limited


def test_without_the_overlay_block_nothing_is_probed(tmp_path: Path) -> None:
    probe = FakeProbe({"onexbot": answer("ok")})
    rig = Rig(tmp_path, "h053", probe, recheck=False)
    rig.limit("onexbot", STALE_STAMP)
    result = rig.tick()
    assert result.limit_recheck == "not_configured"
    assert probe.asked == []
    assert rig.entry("onexbot").limited


def test_a_dry_run_probes_nothing_and_changes_nothing(tmp_path: Path) -> None:
    probe = FakeProbe({"onexbot": answer("ok")})
    rig = Rig(tmp_path, "h053", probe)
    rig.limit("onexbot", STALE_STAMP)
    result = rig.tick(dry_run=True)
    assert probe.asked == []
    assert result.limits_cleared == ()
    assert rig.entry("onexbot").limited


def test_the_pool_executes_a_command_only_for_its_own_fingerprint_and_stamp(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, "h053", None)
    rig.limit("onexbot", STALE_STAMP)
    held = rig.entry("onexbot")

    def command(**changes: Any) -> ModelLimitRecheckCommand:
        fields: dict[str, Any] = {
            "label": "onexbot",
            "fingerprint": held.fingerprint,
            "expected_until": datetime(2026, 11, 13, 11, 0, tzinfo=UTC),
            "action": "clear",
            "probed_at": NOW,
        }
        fields.update(changes)
        return ModelLimitRecheckCommand(**fields)

    for refused in (
        command(fingerprint="00000000"),
        command(expected_until=datetime(2026, 11, 13, 11, 0, 1, tzinfo=UTC)),
    ):
        done = rig.pool.handle(
            ModelClaudeTokenPoolRequest(
                operation="apply_limit_rechecks", limit_commands=(refused,)
            )
        )
        assert done.limits_cleared == ()
        assert rig.entry("onexbot").limited
    done = rig.pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="apply_limit_rechecks", limit_commands=(command(),)
        )
    )
    assert done.limits_cleared == ("onexbot",)
    assert not rig.entry("onexbot").limited


def test_the_result_and_the_pool_file_carry_no_token(tmp_path: Path) -> None:
    rig = Rig(tmp_path, "h053", FakeProbe({"onexbot": answer("ok")}))
    rig.limit("onexbot", STALE_STAMP)
    result = rig.tick()
    pool_file = next((tmp_path / "h053" / "store").glob("*.json"), None)
    assert "sk-ant" not in result.model_dump_json()
    assert pool_file is None or "sk-ant" not in pool_file.read_text()


def test_a_notice_with_its_own_reset_replaces_a_stamp_and_an_estimate_never_shortens(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, "h053", None)
    rig.limit("onexbot", STALE_STAMP)
    rig.pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_limited", label="onexbot", seconds=3600, exact=False
        )
    )
    assert rig.entry("onexbot").until == STALE_STAMP
    rig.pool.handle(
        ModelClaudeTokenPoolRequest(
            operation="mark_limited", label="onexbot", seconds=3600, exact=True
        )
    )
    assert rig.entry("onexbot").until == "2026-11-12T00:00:00Z"


def test_export_hands_the_stored_token_to_the_caller_and_refuses_an_unknown_label(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, "h053", None)
    done = rig.pool.handle(
        ModelClaudeTokenPoolRequest(operation="export", label="onexbot")
    )
    assert done.token is not None
    assert (
        fingerprint(done.token.get_secret_value()) == rig.entry("onexbot").fingerprint
    )
    assert "sk-ant" not in done.model_dump_json()
    with pytest.raises(ClaudeTokenPoolRefusedError):
        rig.pool.handle(ModelClaudeTokenPoolRequest(operation="export", label="nobody"))


def test_the_overlay_refuses_a_reset_pattern_without_its_groups() -> None:
    body = json.loads(overlay().model_dump_json())
    body["limit_recheck"]["probe"]["reset_pattern"] = r"resets (?P<month>[A-Za-z]{3})"
    with pytest.raises(ValidationError):
        ModelRotationOverlay.model_validate(body)
