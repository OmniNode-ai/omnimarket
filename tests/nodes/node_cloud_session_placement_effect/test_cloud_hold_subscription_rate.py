# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rate telemetry stays informational; actual limit endings and zero credit hold placement."""

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
    """Replay a historical rate reading alongside its positive credit reading."""
    shutil.copy(FIXTURE, tmp_path / acc.CLOUD_LEGACY_STATE_FILE)
    recorded = json.loads(FIXTURE.read_text())["accounts"]["h200"]
    registry = _registry("lab-primary", legacy=("h200",))
    _logged_in(tmp_path, registry, NOW)
    assert recorded["rate"]["type"] == "seven_day"
    assert recorded["rate"]["status"] == "rejected"
    assert recorded["rate_refused_until"] == "2026-11-04T22:00:00Z"
    assert recorded["credit"]["remaining"] == 244.0
    assert _slots(tmp_path, registry) == {"lab-primary": recorded["cap"]}
    assert acc.cloud_account_skip(recorded, NOW) is None
    assert (
        acc.cloud_slots(tmp_path, registry, None, fresh_s=FRESH_S, now=NOW)["caps"][
            "lab-primary"
        ]["held"]
        is None
    )
    transports = [ModelCloudTransport(name="on_lab-primary")]
    picked = acc.cloud_pick(tmp_path, registry, transports, fresh_s=FRESH_S, now=NOW)
    assert picked["account"] == "lab-primary"
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
    assert held is None
    entry = _account(tmp_path)
    assert entry["rate"]["type"] == kind
    assert entry["rate"]["status"] == status
    assert "limited_until" not in entry
    assert acc.cloud_account_skip(entry, NOW) is None
    assert _slots(tmp_path, _registry("host_a")) == {"host_a": acc.CLOUD_CAP_START}


@pytest.mark.parametrize("status", ["allowed", "allowed_warning", "rejected"])
@pytest.mark.parametrize("kind", ["five_hour", "seven_day"])
def test_cloud_rate_reading_holds_rejected_when_the_credit_is_unread(
    tmp_path: Path, kind: str, status: str
) -> None:
    """A rejected reading alone never holds an account without a credit reading."""
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
        assert result.held is None
        assert _account(tmp_path, account.label)["rate"]["status"] == "rejected"
    assert _slots(tmp_path, registry) == {
        "host_a": acc.CLOUD_CAP_START,
        "host_b": acc.CLOUD_CAP_START,
    }
    picked = acc.cloud_pick(
        tmp_path,
        registry,
        [ModelCloudTransport(name=f"on_{a.label}") for a in registry],
        fresh_s=FRESH_S,
        now=NOW,
        run_id="must-wait",
    )
    assert picked["account"] == "host_a"
    assert "must-wait" in _account(tmp_path, "host_a")["active"]


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
    assert held is None
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
            "result": {
                "is_error": True,
                "subtype": "error_during_execution",
                "errors": ["Weekly limit reached"],
            },
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
        assert recorded.held is None
    slots = acc.cloud_slots(tmp_path, registry, None, fresh_s=FRESH_S, now=now)
    assert slots["accounts"] == {
        "lab-h051": acc.CLOUD_CAP_START,
        "lab-h053": acc.CLOUD_CAP_START,
    }
    picked = acc.cloud_pick(
        tmp_path,
        registry,
        [ModelCloudTransport(name=f"on_{a.label}") for a in registry],
        fresh_s=FRESH_S,
        now=now,
    )
    assert picked["account"] == "lab-h051"


def test_completed_credit_probe_is_never_a_limit(tmp_path: Path) -> None:
    from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
        cloud_session as ses,
    )

    fixture = json.loads(
        (FIXTURE.parent / "cloud_completed_credit_2026-11-04.json").read_text()
    )
    ended = {**fixture["state"], "result": fixture["events"][-1]}
    assert ses.cloud_session_limited(ended) is False
    result = HandlerCloudSessionPlacement().handle(
        ModelCloudSessionPlacementRequest(
            action="render_result",
            owner="owner",
            account="lab-h053",
            run_id="probe",
            session={"url": "session-url"},
            ended=ended,
            brief="Lane kind: ticket",
        )
    )
    assert result.status == "cloud-no-pr"
    assert "ended on a usage limit" not in result.text
    acc.record_cloud_credit(
        tmp_path,
        "lab-h053",
        fixture["credit"]["remaining"],
        fixture["credit"]["source"],
        now=NOW,
    )
    acc.record_cloud_rate(tmp_path, "lab-h053", ended["rate"], now=NOW)
    assert acc.cloud_account_skip(_account(tmp_path, "lab-h053"), NOW) is None


@pytest.mark.parametrize("completed", [False, True])
def test_completion_result_overrides_stale_limit_text(completed: bool) -> None:
    from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
        cloud_session as ses,
    )

    ended = {"status": "idle", "bucket": "completed", "detail": "Weekly limit reached"}
    if completed:
        ended["result"] = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "terminal_reason": "completed",
        }
    else:
        ended["result"] = {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": ["Weekly limit reached"],
        }
    assert ses.cloud_session_limited(ended) is (not completed)


def test_recorded_zero_credit_blocks_without_exhausted_marker() -> None:
    entry = {
        "credit": {
            "remaining": 0,
            "read_at": "2026-11-04",
            "source": "operator usage page",
        }
    }
    assert "credit read zero" in str(acc.cloud_account_skip(entry, NOW))


def test_recorded_primary_limit_ending_persists_hold_through_handler(
    tmp_path: Path,
) -> None:
    fixture = json.loads(
        (FIXTURE.parent / "cloud_weekly_limit_2026-11-04.json").read_text()
    )
    now = datetime.fromisoformat(fixture["state"]["updated_at"])
    registry = _registry("lab-primary")
    _logged_in(tmp_path, registry, now)
    result = HandlerCloudSessionPlacement().handle(
        ModelCloudSessionPlacementRequest(
            action="render_result",
            placement_dir=str(tmp_path),
            owner="owner",
            account="lab-primary",
            run_id="weekly",
            session={"url": "session-url"},
            ended={**fixture["state"], "result": fixture["result"]},
            now=now,
        )
    )
    assert result.error is None
    assert result.status == "limited"
    assert result.outcome == "blocked"
    assert "usage limit" in result.text
    assert acc.cloud_slots(tmp_path, registry, None, fresh_s=FRESH_S, now=now)[
        "accounts"
    ] == {"lab-primary": 0}
    assert acc.cloud_account_skip(_account(tmp_path, "lab-primary"), now).startswith(
        "limited until"
    )
    acc.record_cloud_credit(
        tmp_path,
        "lab-primary",
        fixture["credit"]["remaining"],
        fixture["credit"]["source"],
        now=now,
    )
    assert acc.cloud_account_skip(_account(tmp_path, "lab-primary"), now).startswith(
        "credit read zero"
    )


@pytest.mark.parametrize("status", ["allowed", "rejected"])
def test_old_rate_inferred_hold_never_retires_credit(status: str) -> None:
    entry = {
        "limited_until": "2026-11-05T13:00:00Z",
        "limited_source": "recorded",
        "limited_why": "seven_day limit rejected",
        "rate": {"type": "five_hour", "status": status},
        "credit": {"remaining": 167, "read_at": "2026-11-04"},
    }
    assert acc.cloud_account_skip(entry, NOW) is None


@pytest.mark.parametrize(
    ("fixture_name", "limited"),
    [
        ("cloud_completed_credit_2026-11-04.json", False),
        ("cloud_weekly_limit_2026-11-04.json", True),
    ],
)
def test_recorded_session_reader(
    tmp_path: Path, monkeypatch, capsys, fixture_name: str, limited: bool
) -> None:
    import sys
    import urllib.request
    from io import BytesIO

    from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
        cloud_session as ses,
    )

    fixture = json.loads((FIXTURE.parent / fixture_name).read_text())
    state = fixture["state"]
    events = fixture.get("events", [fixture.get("result")])
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "test-only"}})
    )
    (tmp_path / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"organizationUuid": "test-only"}})
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    api = "https://api.anthropic.com/v1/sessions/"
    endpoint = api + fixture["session_id"]
    pages = {
        endpoint: {
            "session_status": state["status"],
            "status_bucket": state["bucket"],
            "updated_at": state["updated_at"],
            "post_turn_summary": {"status_detail": state["detail"]},
            "external_metadata": {"rate_limit_info": state["rate"]},
        },
        endpoint + "/events": {
            "data": events[:1],
            "has_more": True,
            "last_id": "first",
        },
        endpoint + "/events?after_id=first": {"data": events[1:], "has_more": False},
    }
    asked = []

    def urlopen(request, timeout):
        asked.append(request.full_url)
        return BytesIO(json.dumps(pages[request.full_url]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "argv", ["-", fixture["session_id"], api])
    handler = HandlerCloudSessionPlacement()
    rendered = handler.handle(
        ModelCloudSessionPlacementRequest(
            action="render_session_script",
            path_export="export PATH=/usr/bin",
            session_id=fixture["session_id"],
            sessions_api_url=api,
        )
    )
    assert rendered.error is None
    program = rendered.text.split("<<'ONEX_CLOUD_SESSION'\n", 1)[1].rsplit(
        "\nONEX_CLOUD_SESSION", 1
    )[0]
    exec(program, {"__name__": "__main__"})
    output = capsys.readouterr().out
    assert asked == list(pages)
    parsed = handler.handle(
        ModelCloudSessionPlacementRequest(action="parse_session", text=output)
    )
    assert parsed.error is None
    assert parsed.parsed["result"] == {
        key: value
        for key, value in events[-1].items()
        if key in {"type", "subtype", "is_error", "terminal_reason", "result", "errors"}
    }
    assert ses.cloud_session_limited(parsed.parsed) is limited
    now = datetime.fromisoformat(state["updated_at"])
    registry = _registry(fixture["account"])
    _logged_in(tmp_path, registry, now)
    recorded = handler.handle(
        ModelCloudSessionPlacementRequest(
            action="record_rate",
            placement_dir=str(tmp_path),
            account=fixture["account"],
            rate=parsed.parsed["rate"],
            now=now,
        )
    )
    assert recorded.error is None
    assert recorded.held is None
    result = handler.handle(
        ModelCloudSessionPlacementRequest(
            action="render_result",
            placement_dir=str(tmp_path),
            owner="owner",
            account=fixture["account"],
            run_id="recorded",
            session={"url": "session-url"},
            ended=parsed.parsed,
            now=now,
        )
    )
    assert result.error is None
    assert result.status == ("limited" if limited else "cloud-no-pr")
    assert ("ended on a usage limit" in result.text) is limited
    assert (
        acc.cloud_account_skip(_account(tmp_path, fixture["account"]), now) is not None
    ) is limited
