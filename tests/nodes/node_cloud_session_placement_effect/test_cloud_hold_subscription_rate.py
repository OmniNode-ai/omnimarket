# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded rejected usage limits hold cloud placement until their reset."""

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.cloud_session_placement import (
    ModelCloudAccount,
    ModelCloudTransport,
)
from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    HandlerCloudSessionPlacement,
)
from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    cloud_accounts as acc,
)
from omnimarket.nodes.node_cloud_session_placement_effect.models import (
    ModelCloudSessionPlacementRequest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "cloud_accounts_h200_2026-11-02.json"
NOW = datetime(2026, 11, 2, 12, 22, tzinfo=UTC)
FRESH_S = 1800


def _registry(
    *labels: str, legacy: tuple[str, ...] = ()
) -> tuple[ModelCloudAccount, ...]:
    return tuple(
        ModelCloudAccount(
            label=label,
            org_hash=f"{i:010x}",
            email_hash=f"{i + 32:010x}",
            consent="2026-01-01T00:00:00Z",
            legacy_hosts=legacy,
        )
        for i, label in enumerate(labels, start=1)
    )


def _logged_in(
    pdir: Path, accounts: tuple[ModelCloudAccount, ...], now: datetime
) -> None:
    """Every account has a fresh passing login on a host of its own name."""
    with acc._cloud_state(pdir, accounts, now) as state:
        for a in accounts:
            state["logins"][a.label] = {
                f"on_{a.label}": {
                    "seen_at": f"{now:%Y-%m-%dT%H:%M:%SZ}",
                    "cloud_ok": True,
                }
            }


def _slots(pdir: Path, accounts: tuple[ModelCloudAccount, ...]) -> dict[str, int]:
    return acc.cloud_slots(pdir, accounts, None, fresh_s=FRESH_S, now=NOW)["accounts"]


def _account(pdir: Path, name: str = "host_a") -> dict[str, Any]:
    return dict(json.loads((pdir / acc.CLOUD_STATE_FILE).read_text())["accounts"][name])


def test_cloud_hold_subscription_rate_recorded_h200_holds_slots_and_pick(
    tmp_path: Path,
) -> None:
    """Replay the recorded rejected seven-day reading despite positive cloud credit."""
    shutil.copy(FIXTURE, tmp_path / acc.CLOUD_LEGACY_STATE_FILE)
    recorded = json.loads(FIXTURE.read_text())["accounts"]["h200"]
    registry = _registry("lab-primary", legacy=("h200",))
    _logged_in(tmp_path, registry, NOW)
    assert recorded["rate"]["type"] == "seven_day"
    assert recorded["rate"]["status"] == "rejected"
    assert recorded["rate_refused_until"] == "2026-11-04T22:00:00Z"
    assert recorded["credit"]["remaining"] == 244.0
    assert _slots(tmp_path, registry) == {"lab-primary": 0}
    assert "2026-11-04T22:00:00Z" in str(acc.cloud_account_skip(recorded, NOW))
    assert (
        acc.cloud_slots(tmp_path, registry, None, fresh_s=FRESH_S, now=NOW)["caps"][
            "lab-primary"
        ]["held"]
        is not None
    )
    transports = [ModelCloudTransport(name="on_lab-primary")]
    picked = acc.cloud_pick(tmp_path, registry, transports, fresh_s=FRESH_S, now=NOW)
    assert picked["account"] is None
    reset = datetime(2026, 11, 4, 22, tzinfo=UTC)
    _logged_in(tmp_path, registry, reset)
    assert (
        acc.cloud_pick(tmp_path, registry, transports, fresh_s=FRESH_S, now=reset)[
            "account"
        ]
        == "lab-primary"
    )


@pytest.mark.parametrize("status", ["allowed", "allowed_warning", "rejected"])
@pytest.mark.parametrize("kind", ["five_hour", "seven_day"])
def test_cloud_rate_reading_holds_only_rejected(
    tmp_path: Path, kind: str, status: str
) -> None:
    resets = (NOW + timedelta(days=2)).timestamp()
    acc.record_cloud_credit(
        tmp_path, "host_a", 244.0, "usage reading", now=NOW - timedelta(hours=1)
    )
    _logged_in(tmp_path, _registry("host_a"), NOW)
    held = acc.record_cloud_rate(
        tmp_path,
        "host_a",
        {"rateLimitType": kind, "status": status, "resetsAt": resets},
        now=NOW,
    )
    assert held == ("2026-11-04T12:22:00Z" if status == "rejected" else None)
    entry = _account(tmp_path)
    assert entry["rate"]["type"] == kind
    assert entry["rate"]["status"] == status
    if status == "rejected":
        assert entry["limited_until"] == held
        assert entry["limited_source"] == acc.CLOUD_HOLD_STAMP
        assert acc.cloud_account_skip(entry, NOW) is not None
    else:
        assert "limited_until" not in entry
        assert acc.cloud_account_skip(entry, NOW) is None
    assert _slots(tmp_path, _registry("host_a")) == {
        "host_a": 0 if status == "rejected" else acc.CLOUD_CAP_START
    }


@pytest.mark.parametrize("status", ["allowed", "allowed_warning", "rejected"])
@pytest.mark.parametrize("kind", ["five_hour", "seven_day"])
def test_cloud_rate_reading_holds_rejected_when_the_credit_is_unread(
    tmp_path: Path, kind: str, status: str
) -> None:
    """A rejected reading also holds an account without a credit reading."""
    acc.record_cloud_rate(
        tmp_path, "host_a", {"rateLimitType": kind, "status": status}, now=NOW
    )
    assert (acc.cloud_account_skip(_account(tmp_path), NOW) is not None) == (
        status == "rejected"
    )


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
    registry = _registry("host_a")
    _logged_in(tmp_path, registry, NOW)
    acc.cloud_pick(
        tmp_path,
        registry,
        [ModelCloudTransport(name="on_host_a")],
        fresh_s=FRESH_S,
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
    assert _slots(tmp_path, registry) == {"host_a": 0}
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


def test_record_rate_handler_persists_before_return_and_all_rejected_wait(
    tmp_path: Path,
) -> None:
    registry = _registry("host_a", "host_b")
    _logged_in(tmp_path, registry, NOW)
    handler = HandlerCloudSessionPlacement()
    for account in registry:
        result = handler.handle(
            ModelCloudSessionPlacementRequest(
                action="record_rate",
                placement_dir=str(tmp_path),
                account=account.label,
                now=NOW,
                rate={
                    "rateLimitType": "seven_day",
                    "status": "rejected",
                    "resetsAt": (NOW + timedelta(days=2)).timestamp(),
                },
            )
        )
        assert result.error is None
        assert result.held == "2026-11-04T12:22:00Z"
        assert _account(tmp_path, account.label)["limited_until"] == result.held
    assert _slots(tmp_path, registry) == {"host_a": 0, "host_b": 0}
    picked = acc.cloud_pick(
        tmp_path,
        registry,
        [ModelCloudTransport(name=f"on_{a.label}") for a in registry],
        fresh_s=FRESH_S,
        now=NOW,
        run_id="must-wait",
    )
    assert picked["account"] is None
    assert all(
        "must-wait" not in _account(tmp_path, a.label).get("active", {})
        for a in registry
    )


def test_rejected_rate_does_not_shorten_an_existing_hold_or_outlive_reset(
    tmp_path: Path,
) -> None:
    acc.mark_cloud_limited(
        tmp_path, "host_a", "2026-11-05T00:00:00Z", "recorded refusal"
    )
    held = acc.record_cloud_rate(
        tmp_path,
        "host_a",
        {
            "status": "rejected",
            "resetsAt": (NOW + timedelta(hours=1)).timestamp(),
        },
        now=NOW,
    )
    assert held == "2026-11-05T00:00:00Z"
    assert _account(tmp_path)["limited_why"] == "recorded refusal"
    for seconds in (-1, 0):
        acc.record_cloud_rate(
            tmp_path,
            "host_b",
            {
                "status": "rejected",
                "resetsAt": (NOW + timedelta(seconds=seconds)).timestamp(),
            },
            now=NOW,
        )
        assert acc.cloud_account_skip(_account(tmp_path, "host_b"), NOW) is None


@pytest.mark.parametrize(
    "ended",
    [
        {
            "status": "idle",
            "rate": {"status": "rejected", "rateLimitType": "seven_day"},
        },
        {
            "status": "requires_action",
            "detail": "Weekly limit reached, resets 2026-11-04T22:00:00Z",
        },
        {
            "status": "idle",
            "needs_action": "You've hit your limit · resets Nov 5 at 9am (UTC)",
        },
        {"status": "archived", "bucket": "usage_limit"},
    ],
)
@pytest.mark.parametrize("brief", ["Lane kind: ticket", "Lane kind: review-only"])
def test_terminal_usage_limit_is_limited_through_handler(
    tmp_path: Path, ended: dict, brief: str
) -> None:
    result = HandlerCloudSessionPlacement().handle(
        ModelCloudSessionPlacementRequest(
            action="render_result",
            owner="owner",
            account="host_a",
            run_id="run-a",
            session={"url": "session-url"},
            ended=ended,
            brief=brief,
        )
    )
    assert result.error is None
    assert result.status == "limited"
    assert result.outcome == "blocked"
    assert "usage limit" in str(result.text).lower()
    assert "watcher showed no" not in str(result.text)
    assert "LANE_RESULT outcome=blocked" in str(result.text)


@pytest.mark.parametrize(
    "ended",
    [
        {
            "status": "idle",
            "detail": "Finished the work",
            "rate": {"status": "allowed"},
        },
        {"status": "idle", "detail": "too many concurrent sessions"},
        {"status": "running", "detail": "weekly limit"},
    ],
)
def test_other_sessions_still_close_without_a_pr(tmp_path: Path, ended: dict) -> None:
    result = HandlerCloudSessionPlacement().handle(
        ModelCloudSessionPlacementRequest(
            action="render_result",
            owner="owner",
            account="host_a",
            run_id="run-a",
            session={"url": "session-url"},
            ended=ended,
        )
    )
    assert result.status == "cloud-no-pr"


def test_a_pr_keeps_its_handoff_when_session_ends_limited(tmp_path: Path) -> None:
    result = HandlerCloudSessionPlacement().handle(
        ModelCloudSessionPlacementRequest(
            action="render_result",
            owner="owner",
            account="host_a",
            run_id="run-a",
            session={"url": "session-url"},
            ended={"status": "idle", "detail": "weekly limit"},
            prs=({"pr": "repo#1", "head": "abc"},),
        )
    )
    assert result.status == "done"
    assert "LANE_RESULT outcome=handed-off" in str(result.text)


def test_recorded_pool_session_rates_offer_only_the_allowed_account(
    tmp_path: Path,
) -> None:
    receipt = json.loads(
        (FIXTURE.parent / "cloud_session_rates_2026-11-04.json").read_text()
    )
    now = datetime.fromisoformat(receipt["observed_at"])
    registry = _registry(*receipt["sessions"])
    _logged_in(tmp_path, registry, now)
    handler = HandlerCloudSessionPlacement()
    for label, session in receipt["sessions"].items():
        parsed = handler.handle(
            ModelCloudSessionPlacementRequest(
                action="parse_session", text="CLOUD-SESSION " + json.dumps(session)
            )
        )
        assert parsed.error is None
        assert parsed.parsed is not None
        recorded = handler.handle(
            ModelCloudSessionPlacementRequest(
                action="record_rate",
                placement_dir=str(tmp_path),
                account=label,
                rate=parsed.parsed["rate"],
                now=now,
            )
        )
        assert recorded.error is None
        assert recorded.held == (
            "2026-11-05T13:00:00Z" if label == "lab-h053" else None
        )
    slots = acc.cloud_slots(tmp_path, registry, None, fresh_s=FRESH_S, now=now)
    assert slots["accounts"] == {"lab-h051": acc.CLOUD_CAP_START, "lab-h053": 0}
    picked = acc.cloud_pick(
        tmp_path,
        registry,
        [ModelCloudTransport(name=f"on_{a.label}") for a in registry],
        fresh_s=FRESH_S,
        now=now,
    )
    assert picked["account"] == "lab-h051"
