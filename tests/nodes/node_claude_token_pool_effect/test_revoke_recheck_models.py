# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The revoke-recheck overlay block and the mirror keys it reads (OMN-19998).

The overlay accepts ``revoke_recheck`` only alongside ``limit_recheck``, whose probe it re-uses; the
settings refuse an interval under a minute and a blank challenge marker; the mirror reader carries the
pool's ``revoke_cause`` and ``probed_at`` through, and an older mirror without the keys reads ``None``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_claude_pool_revoke_recheck import (
    ModelRevokeRecheckPolicy,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.adapter_rotation_ports import (
    MirrorMarksSource,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRevokeRecheckSettings,
    ModelRotationMark,
    ModelRotationOverlay,
)

pytestmark = pytest.mark.unit

INTERVAL = 3600
PROBED = "2026-11-10T09:30:00Z"


def probe_block() -> dict[str, Any]:
    return {
        "model": "sonnet",
        "prompt": "Reply with the single word ok",
        "timeout_seconds": 90,
        "limited_patterns": ["hit your weekly limit"],
        "reset_pattern": r"resets (?P<month>[A-Za-z]{3}) (?P<day>\d{1,2}), (?P<hour>\d{1,2})(?P<ampm>am|pm)",
        "reset_margin_seconds": 7,
    }


def overlay_body(
    *,
    limit: bool = True,
    revoke: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    if limit:
        body["limit_recheck"] = {
            "policy": {"interval_seconds": INTERVAL},
            "probe": probe_block(),
        }
    if revoke is not None:
        body["revoke_recheck"] = revoke
    return body


def test_the_overlay_accepts_revoke_recheck_alongside_limit_recheck() -> None:
    revoke = {
        "policy": {"interval_seconds": INTERVAL},
        "challenge_markers": ["verify you are human", "unusual activity"],
    }
    overlay = ModelRotationOverlay.model_validate(overlay_body(revoke=revoke))
    assert overlay.revoke_recheck == ModelRevokeRecheckSettings(
        policy=ModelRevokeRecheckPolicy(interval_seconds=INTERVAL),
        challenge_markers=("verify you are human", "unusual activity"),
    )
    # the markers are optional: the probe answers are then all taken at face value
    bare = ModelRotationOverlay.model_validate(
        overlay_body(revoke={"policy": {"interval_seconds": INTERVAL}})
    )
    assert bare.revoke_recheck == ModelRevokeRecheckSettings(
        policy=ModelRevokeRecheckPolicy(interval_seconds=INTERVAL)
    )


def test_the_overlay_refuses_revoke_recheck_without_limit_recheck() -> None:
    body = overlay_body(limit=False, revoke={"policy": {"interval_seconds": INTERVAL}})
    with pytest.raises(ValidationError) as raised:
        ModelRotationOverlay.model_validate(body)
    assert "revoke_recheck is declared but limit_recheck is not" in str(raised.value)


def test_the_settings_refuse_an_interval_under_a_minute() -> None:
    with pytest.raises(ValidationError):
        ModelRevokeRecheckSettings(
            policy=ModelRevokeRecheckPolicy(interval_seconds=59),
            challenge_markers=("verify you are human",),
        )


def test_the_settings_refuse_a_blank_marker() -> None:
    with pytest.raises(ValidationError):
        ModelRevokeRecheckSettings(
            policy=ModelRevokeRecheckPolicy(interval_seconds=INTERVAL),
            challenge_markers=("verify you are human", "   "),
        )


def test_control_a_probed_at_that_is_not_an_instant_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelRotationMark(label="onexbot", probed_at="not a time")
    # an offset is normalised to the Z form, exactly like revoked_at
    mark = ModelRotationMark(label="onexbot", probed_at="2026-11-10T11:30:00+02:00")
    assert mark.probed_at == PROBED


def write_mirror(home: Path, rows: list[dict[str, Any]]) -> None:
    mirror = home / ".cache" / "omni" / "claude-token-pool.json"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    mirror.write_text(
        json.dumps({"host": "h053", "at": "2026-11-10T12:00:00Z", "tokens": rows}),
        encoding="utf-8",
    )


def read_marks(home: Path) -> tuple[ModelRotationMark, ...]:
    return (
        MirrorMarksSource(
            home=home, mirror_path=".cache/omni/claude-token-pool.json", ssh_target=None
        )
        .read()
        .marks
    )


def test_the_mirror_reader_reads_the_revoke_cause_and_the_probe_time(
    tmp_path: Path,
) -> None:
    write_mirror(
        tmp_path,
        [
            {
                "label": "onexbot",
                "until": None,
                "revoked": True,
                "fingerprint": "deadbeef",
                "revoked_at": "2026-11-02T07:25:20Z",
                "limited_at": None,
                "probed_at": PROBED,
                "revoke_cause": "challenged",
            }
        ],
    )
    mark = read_marks(tmp_path)[0]
    assert (mark.revoke_cause, mark.probed_at) == ("challenged", PROBED)


def test_control_an_older_mirror_row_without_the_keys_reads_none(
    tmp_path: Path,
) -> None:
    write_mirror(
        tmp_path,
        [
            {
                "label": "onexbot",
                "until": None,
                "revoked": True,
                "fingerprint": "deadbeef",
                "revoked_at": "2026-11-02T07:25:20Z",
                "limited_at": None,
            }
        ],
    )
    mark = read_marks(tmp_path)[0]
    assert (mark.revoke_cause, mark.probed_at) == (None, None)
