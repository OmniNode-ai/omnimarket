# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Account placement behavior using explicit caller supplied host rows."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    HandlerCloudSessionPlacement,
)
from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    cloud_accounts as acc,
)
from omnimarket.nodes.node_cloud_session_placement_effect.models import (
    ModelCloudHost,
    ModelCloudSessionPlacementRequest,
)

CONSENT = "2026-01-01T00:00:00Z"


def host_pool() -> list[ModelCloudHost]:
    return [
        ModelCloudHost(name=name, engines=("claude_sonnet",), cloud_consent=CONSENT)
        for name in ("host_a", "host_b", "host_c")
    ] + [ModelCloudHost(name="host_no_engine")]


def test_accounts_are_taken_round_robin_and_a_login_without_cloud_access_is_passed_over(
    tmp_path,
) -> None:
    pdir = tmp_path / "placement"
    answers = {
        "host_a": acc.CLOUD_LOGIN_OK,
        "host_b": acc.CLOUD_LOGIN_OK,
        "host_c": acc.CLOUD_LOGIN_NONE,
    }

    def probe(host: Any) -> str:
        return answers[host.name]

    hosts = host_pool()
    picks = [acc.cloud_pick(pdir, hosts, probe)[0].name for _ in range(3)]
    assert picks == ["host_a", "host_b", "host_a"]
    state = json.loads((pdir / acc.CLOUD_STATE_FILE).read_text())
    assert state["accounts"]["host_c"]["login"] == acc.CLOUD_LOGIN_NONE
    assert state["last"] == "host_a"
    assert "host_no_engine" not in state["accounts"]


def test_the_least_loaded_account_is_picked_and_a_fresh_login_verdict_is_not_probed_again(
    tmp_path,
) -> None:

    pdir, now = tmp_path / "placement", datetime(2026, 10, 21, 18, 50, tzinfo=UTC)
    probes: list[str] = []

    def probe(host: Any) -> str:
        probes.append(host.name)
        return acc.CLOUD_LOGIN_NONE if host.name == "host_c" else acc.CLOUD_LOGIN_OK

    hosts = host_pool()
    picks = [
        acc.cloud_pick(pdir, hosts, probe, now=now, run_id=f"r{i}")[0].name
        for i in range(4)
    ]
    assert sorted(picks) == ["host_a", "host_a", "host_b", "host_b"]
    assert sorted(probes) == ["host_a", "host_b", "host_c"]
    acc.record_cloud_run(
        pdir, "host_b", {"run_id": f"r{picks.index('host_b')}", "session_id": "s"}
    )
    assert acc.cloud_pick(pdir, hosts, probe, now=now, run_id="r9")[0].name == "host_b"
    later = now + acc.CLOUD_LOGIN_FRESH + timedelta(minutes=1)
    acc.cloud_pick(pdir, hosts, probe, now=later, run_id="r10")
    assert probes.count("host_c") == 2


def test_an_account_whose_credit_reads_zero_is_never_used_again(tmp_path) -> None:
    pdir = tmp_path / "placement"
    acc.record_cloud_credit(pdir, "host_a", 0.0, "operator reading")
    host, tried = acc.cloud_pick(pdir, host_pool(), lambda _h: acc.CLOUD_LOGIN_OK)
    assert host.name == "host_b"
    assert {"account": "host_a", "skipped": tried[0]["skipped"]} == tried[0]
    assert "credit read zero" in tried[0]["skipped"]
    acc.record_cloud_credit(pdir, "host_b", 0.0, "operator reading")
    acc.record_cloud_credit(pdir, "host_c", -1.0, "operator reading")
    assert acc.cloud_pick(pdir, host_pool(), lambda _h: acc.CLOUD_LOGIN_OK)[0] is None


def test_the_accounts_file_never_waits_on_the_placement_lock(tmp_path) -> None:

    import fcntl

    pdir = tmp_path / "placement"
    pdir.mkdir()
    with (pdir / ".lock").open("a+", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        acc.record_cloud_credit(pdir, "host_a", 120.0, "operator reading")
        host, _tried = acc.cloud_pick(pdir, host_pool(), lambda _h: acc.CLOUD_LOGIN_OK)
        acc.mark_cloud_limited(pdir, "host_a", "2026-10-25T23:00:00Z")
    assert host is not None
    assert (pdir / acc.CLOUD_LOCK_FILE).exists()
    assert acc.CLOUD_LOCK_FILE != ".lock"


def test_an_account_runs_up_to_its_cap_and_has_no_daily_bound(tmp_path) -> None:

    pdir, ok = tmp_path / "placement", (lambda _h: acc.CLOUD_LOGIN_OK)
    now = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
    hosts = [h for h in host_pool() if h.name == "host_a"]
    picked = [
        acc.cloud_pick(pdir, hosts, ok, now=now, run_id=f"r{i}")[0]
        for i in range(acc.CLOUD_CAP_START + 1)
    ]
    assert [p.name if p else None for p in picked] == [
        "host_a"
    ] * acc.CLOUD_CAP_START + [None]
    assert acc.cloud_slots(pdir, now, consented={"host_a"}) == {"host_a": 0}
    host, tried = acc.cloud_pick(pdir, hosts, ok, now=now, run_id="r-none")
    assert host is None
    assert "at its cap: 4 running (cap 4)" in tried[0]["skipped"]

    for i in range(40):
        acc.record_cloud_run(
            pdir,
            "host_a",
            {"run_id": f"r{i % acc.CLOUD_CAP_START}", "session_id": f"session_{i}"},
        )
        assert acc.cloud_pick(
            pdir,
            hosts,
            ok,
            now=now + timedelta(minutes=i),
            run_id=f"r{i % acc.CLOUD_CAP_START}",
        )[0]
    assert not hasattr(acc, "CLOUD_STARTS_PER_DAY")

    assert acc.cloud_slots(
        pdir, now + acc.CLOUD_ACTIVE_TTL + timedelta(hours=1), consented={"host_a"}
    ) == {"host_a": acc.CLOUD_CAP_START}


def test_the_cap_rises_while_starts_succeed_at_it_and_falls_to_the_running_count_on_a_refusal(
    tmp_path,
) -> None:
    pdir, ok = tmp_path / "placement", (lambda _h: acc.CLOUD_LOGIN_OK)
    now = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
    hosts = [h for h in host_pool() if h.name == "host_a"]
    for i in range(acc.CLOUD_CAP_START):
        acc.cloud_pick(pdir, hosts, ok, now=now, run_id=f"s{i}")
        cap = acc.note_cloud_start(pdir, "host_a", now=now)
    assert cap == acc.CLOUD_CAP_START + 1
    assert acc.cloud_slots(pdir, now, consented={"host_a"}) == {"host_a": 1}
    acc.cloud_pick(pdir, hosts, ok, now=now, run_id="s-refused")
    assert acc.cloud_start_refused(
        "Error: too many concurrent sessions for this account"
    )
    assert not acc.cloud_start_refused(
        "Quick safety check: Is this a project you trust?"
    )
    assert (
        acc.note_cloud_refusal(pdir, "host_a", "s-refused", "too many", now=now)
        == acc.CLOUD_CAP_START
    )
    assert acc.cloud_slots(pdir, now, consented={"host_a"}) == {"host_a": 0}

    acc.record_cloud_run(pdir, "host_a", {"run_id": "s0", "session_id": "session_0"})
    acc.cloud_pick(pdir, hosts, ok, now=now, run_id="s4")
    assert (
        acc.note_cloud_start(pdir, "host_a", now=now + timedelta(minutes=5))
        == acc.CLOUD_CAP_START
    )
    assert acc.note_cloud_start(
        pdir, "host_a", now=now + acc.CLOUD_REFUSAL_HOLD + timedelta(minutes=1)
    ) == (acc.CLOUD_CAP_START + 1)


def test_a_session_reading_off_the_cloud_credit_at_a_warning_holds_the_account(
    tmp_path,
) -> None:

    pdir, now = tmp_path / "placement", datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
    assert (
        acc.record_cloud_rate(
            pdir,
            "host_c",
            {
                "rateLimitType": "ccr_promotional",
                "status": "allowed_warning",
                "resetsAt": 1793865600,
            },
            now=now,
        )
        is None
    )
    why = acc.record_cloud_rate(
        pdir,
        "host_b",
        {
            "rateLimitType": "seven_day",
            "status": "allowed_warning",
            "resetsAt": 1792753200,
        },
        now=now,
    )
    assert why
    assert "seven_day" in why
    state = json.loads((pdir / acc.CLOUD_STATE_FILE).read_text())["accounts"]
    assert state["host_b"]["limited_until"] == "2026-10-23T11:00:00Z"
    assert "limited_until" not in state["host_c"]
    assert "seven_day" in acc.cloud_account_skip(state["host_b"], now)
    assert acc.record_cloud_rate(
        pdir,
        "host_c",
        {"rateLimitType": "ccr_promotional", "status": "rejected"},
        now=now,
    )


def test_cloud_cost_is_the_drop_between_readings_around_the_run_else_unreadable() -> (
    None
):
    before = {"remaining": 250.0, "read_at": "2026-10-17T00:00:00Z"}
    after = {"remaining": 243.5, "read_at": "2026-10-17T01:00:00Z"}
    assert acc.cloud_cost(before, after, "2026-10-17T00:30:00Z")["credit_used"] == 6.5
    unread = acc.cloud_cost(None, None, "2026-10-17T00:30:00Z")
    assert unread["credit_used"] is None
    assert unread["credit_read"].startswith("unreadable")
    assert acc.cloud_cost(before, before, "2026-10-17T00:30:00Z")["credit_used"] is None


@pytest.mark.parametrize(
    ("over", "refusal"),
    [
        ({"push": False, "cloud_consent": CONSENT}, acc.CLOUD_NO_PUSH),
        ({"contributor": True}, acc.CLOUD_CONTRIBUTOR),
        ({}, acc.CLOUD_NO_CONSENT),
        ({"cloud_consent": "yes please"}, "is not a ledger row stamp"),
    ],
)
def test_consent_refusals(over: dict[str, Any], refusal: str) -> None:
    host = ModelCloudHost(name="host_a", **over)
    assert refusal in str(acc.cloud_consent_refusal(host))
    assert not acc.consented_accounts([host])


def test_explicit_consent_admits_a_contributor() -> None:
    host = ModelCloudHost(name="host_a", contributor=True, cloud_consent=CONSENT)
    assert acc.cloud_consent_refusal(host) is None
    assert acc.consented_accounts([host]) == {"host_a"}


def test_contributor_is_never_probed_picked_or_counted(tmp_path: Path) -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    with acc._cloud_state(tmp_path) as state:
        state["accounts"]["host_c"] = {
            "login": "ok",
            "probed_at": "2026-01-02T00:00:00Z",
            "cap": 10,
            "rate": {"type": "ccr_promotional", "status": "allowed"},
        }
    hosts = [
        *host_pool()[:2],
        ModelCloudHost(name="host_c", engines_live=True, contributor=True),
    ]
    probed = []

    def probe(host: ModelCloudHost) -> str:
        probed.append(host.name)
        return acc.CLOUD_LOGIN_OK

    for i in range(10):
        host, tried = acc.cloud_pick(tmp_path, hosts, probe, now=now, run_id=f"r{i}")
        assert host is None or host.name != "host_c"
        assert {"account": "host_c", "skipped": acc.CLOUD_CONTRIBUTOR} in tried
    assert "host_c" not in probed
    consented = acc.consented_accounts(hosts)
    assert "host_c" not in acc.cloud_slots(tmp_path, now, consented=consented)
    assert "host_c" not in acc.cloud_caps(tmp_path, now, consented=consented)
    handler = HandlerCloudSessionPlacement()
    assert (
        handler.handle(
            ModelCloudSessionPlacementRequest(
                action="slots", placement_dir=str(tmp_path), now=now
            )
        ).free
        == 0
    )
    result = handler.handle(
        ModelCloudSessionPlacementRequest(
            action="slots", placement_dir=str(tmp_path), now=now, hosts=tuple(hosts)
        )
    )
    assert "host_c" not in result.accounts


def test_engines_and_custom_engine_selection(tmp_path: Path) -> None:
    hosts = [
        ModelCloudHost(name="host_a", cloud_consent=CONSENT),
        ModelCloudHost(name="host_b", engines=("other",), cloud_consent=CONSENT),
    ]
    assert acc.cloud_pick(tmp_path, hosts, lambda _: "ok")[0] is None
    assert (
        acc.cloud_pick(tmp_path, hosts, lambda _: "ok", claude_engines=("other",))[
            0
        ].name
        == "host_b"
    )
