# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cloud sessions draw on the cloud credit: a subscription rate reading never gates placement."""

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    cloud_accounts as acc,
)
from omnimarket.nodes.node_cloud_session_placement_effect.models import ModelCloudHost

FIXTURE = Path(__file__).parent / "fixtures" / "cloud_accounts_h200_2026-11-02.json"
NOW = datetime(2026, 11, 2, 12, 22, tzinfo=UTC)
HOST = ModelCloudHost(
    name="host_a", engines=("claude_sonnet",), cloud_consent="2026-01-01T00:00:00Z"
)


def _account(pdir: Path, name: str = "host_a") -> dict[str, Any]:
    return dict(json.loads((pdir / acc.CLOUD_STATE_FILE).read_text())["accounts"][name])


def test_cloud_hold_subscription_rate_recorded_h200_offers_slots(
    tmp_path: Path,
) -> None:
    """The h200 record of 2026-11-02 (seven_day rejected, hold to 2026-11-04T22:00Z, credit 244) offers slots."""
    shutil.copy(FIXTURE, tmp_path / acc.CLOUD_STATE_FILE)
    recorded = _account(tmp_path, "h200")
    assert recorded["rate"]["type"] == "seven_day"
    assert recorded["rate"]["status"] == "rejected"
    assert recorded["rate_refused_until"] == "2026-11-04T22:00:00Z"
    assert recorded["credit"]["remaining"] == 244.0
    assert acc.cloud_slots(tmp_path, NOW, consented={"h200"}) == {"h200": 17}
    assert acc.cloud_account_skip(recorded, NOW) is None
    assert acc.cloud_caps(tmp_path, NOW, consented={"h200"})["h200"]["held"] is None


@pytest.mark.parametrize("status", ["allowed", "allowed_warning", "rejected"])
@pytest.mark.parametrize("kind", ["five_hour", "seven_day"])
def test_cloud_rate_reading_never_holds(tmp_path: Path, kind: str, status: str) -> None:
    resets = (NOW + timedelta(days=2)).timestamp()
    acc.record_cloud_credit(
        tmp_path, "host_a", 244.0, "usage reading", now=NOW - timedelta(hours=1)
    )
    with acc._cloud_state(tmp_path) as state:
        state["accounts"]["host_a"]["login"] = acc.CLOUD_LOGIN_OK
    held = acc.record_cloud_rate(
        tmp_path,
        "host_a",
        {"rateLimitType": kind, "status": status, "resetsAt": resets},
        now=NOW,
    )
    assert held is None
    entry = _account(tmp_path)
    # The reading stays as telemetry, and no key that gates placement was written.
    assert entry["rate"]["type"] == kind
    assert entry["rate"]["status"] == status
    assert not {"limited_until", "limited_why", "rate_refused_until"} & set(entry)
    assert acc.cloud_account_skip(entry, NOW) is None
    assert acc.cloud_slots(tmp_path, NOW, consented={"host_a"}) == {
        "host_a": acc.CLOUD_CAP_START
    }


@pytest.mark.parametrize("status", ["allowed", "allowed_warning", "rejected"])
@pytest.mark.parametrize("kind", ["five_hour", "seven_day"])
def test_cloud_rate_reading_never_holds_when_the_credit_is_unread(
    tmp_path: Path, kind: str, status: str
) -> None:
    """Only the credit gates: an account with no credit reading is not held by a rate header either."""
    acc.record_cloud_rate(
        tmp_path, "host_a", {"rateLimitType": kind, "status": status}, now=NOW
    )
    assert acc.cloud_account_skip(_account(tmp_path), NOW) is None


def test_cloud_rate_reading_never_holds_the_credit_gate_stays(tmp_path: Path) -> None:
    acc.record_cloud_credit(tmp_path, "host_a", 0.0, "usage reading", now=NOW)
    acc.record_cloud_rate(
        tmp_path, "host_a", {"rateLimitType": "five_hour", "status": "allowed"}, now=NOW
    )
    assert "credit read zero" in str(acc.cloud_account_skip(_account(tmp_path), NOW))


def test_cloud_rate_reading_never_holds_an_old_rate_derived_hold_is_not_honored() -> (
    None
):
    """A hold the node wrote from a rate header before this fix carries no recorded-hold stamp."""
    entry = {
        "limited_until": "2026-11-04T22:00:00Z",
        "limited_why": "five_hour limit rejected",
        "rate_refused_until": "2026-11-04T22:00:00Z",
        "rate_refused_why": "five_hour limit rejected",
        "promotional_fallback_at": "2026-11-02T09:00:00Z",
        "login": acc.CLOUD_LOGIN_OK,
    }
    assert acc.cloud_account_skip(entry, NOW) is None


def test_cloud_measured_refusal_holds_until_the_named_reset(tmp_path: Path) -> None:
    text = "Error: usage limit reached, resets 2026-11-03T04:30:00Z"
    acc.cloud_pick(
        tmp_path,
        [HOST],
        lambda _h: acc.CLOUD_LOGIN_OK,
        now=NOW,
        run_id="r1",
    )
    assert acc.cloud_start_refused(text)
    acc.note_cloud_refusal(tmp_path, "host_a", "r1", text, now=NOW)
    entry = _account(tmp_path)
    assert entry["limited_until"] == "2026-11-03T04:30:00Z"
    assert entry["limited_why"] == text
    assert (
        acc.cloud_account_skip(entry, NOW)
        == f"limited until 2026-11-03T04:30:00Z ({text})"
    )
    assert acc.cloud_slots(tmp_path, NOW, consented={"host_a"}) == {"host_a": 0}
    after = datetime(2026, 11, 3, 4, 31, tzinfo=UTC)
    assert acc.cloud_account_skip(entry, after) is None


def test_cloud_measured_refusal_holds_for_the_default_hour_when_no_reset_is_named(
    tmp_path: Path,
) -> None:
    text = "429 rate limit exceeded for your plan"
    acc.note_cloud_refusal(tmp_path, "host_a", "r1", text, now=NOW)
    entry = _account(tmp_path)
    assert (
        entry["limited_until"] == f"{NOW + acc.CLOUD_REFUSAL_HOLD:%Y-%m-%dT%H:%M:%SZ}"
    )
    assert entry["limited_why"] == text


def test_cloud_measured_refusal_holds_not_for_a_concurrency_refusal(
    tmp_path: Path,
) -> None:
    acc.note_cloud_refusal(
        tmp_path, "host_a", "r1", "too many concurrent sessions", now=NOW
    )
    entry = _account(tmp_path)
    assert "limited_until" not in entry
    assert entry["refused_why"] == "too many concurrent sessions"


def test_cloud_measured_refusal_holds_a_past_reset_falls_back_to_the_hour(
    tmp_path: Path,
) -> None:
    acc.note_cloud_refusal(
        tmp_path, "host_a", "r1", "usage limit, resets 2026-11-01T00:00:00Z", now=NOW
    )
    assert _account(tmp_path)["limited_until"] == "2026-11-02T13:22:00Z"


def test_cloud_measured_refusal_holds_a_deliberate_mark_is_honored(
    tmp_path: Path,
) -> None:
    acc.mark_cloud_limited(tmp_path, "host_a", "2099-01-01T00:00:00Z", "operator hold")
    assert "operator hold" in str(acc.cloud_account_skip(_account(tmp_path), NOW))
