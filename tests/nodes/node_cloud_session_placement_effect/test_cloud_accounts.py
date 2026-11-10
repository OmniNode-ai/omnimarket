# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Account placement behavior: usage state per account, picks over observed logins."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.models.cloud_session_placement import (
    ModelCloudAccount,
    ModelCloudTransport,
)
from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    cloud_accounts as acc,
)

FRESH_S = 1800
ACCOUNTS = tuple(
    ModelCloudAccount(
        label=label,
        org_hash=f"{i:010x}",
        email_hash=f"{i + 16:010x}",
        consent="2026-01-01T00:00:00Z",
    )
    for i, label in enumerate(("acct_a", "acct_b", "acct_c"), start=1)
)
HOSTS = tuple(ModelCloudTransport(name=f"host_{c}") for c in "abc")


def _login(pdir: Path, now: datetime, ok: dict[str, bool]) -> None:
    """Record one login per account on the host of the same letter, with the given probe verdicts."""
    # The raw-text path is covered in test_cloud_account_registry; here the login map is written directly.
    with acc._cloud_state(pdir, ACCOUNTS, now) as state:
        for a in ACCOUNTS:
            state["logins"][a.label] = {
                f"host_{a.label[-1]}": {
                    "seen_at": f"{now:%Y-%m-%dT%H:%M:%SZ}",
                    "cloud_ok": ok[a.label],
                }
            }


def _pick(
    pdir: Path,
    now: datetime,
    run_id: str = "",
    accounts: tuple[ModelCloudAccount, ...] = ACCOUNTS,
) -> dict:
    return acc.cloud_pick(
        pdir, accounts, HOSTS, fresh_s=FRESH_S, now=now, run_id=run_id
    )


NOW = datetime(2026, 10, 21, 18, 50, tzinfo=UTC)
ALL_OK = {a.label: True for a in ACCOUNTS}


def test_accounts_are_taken_round_robin_and_a_login_without_cloud_access_is_passed_over(
    tmp_path,
) -> None:
    pdir = tmp_path / "placement"
    _login(pdir, NOW, {"acct_a": True, "acct_b": True, "acct_c": False})
    picks = [_pick(pdir, NOW)["account"] for _ in range(3)]
    assert picks == ["acct_a", "acct_b", "acct_a"]
    state = json.loads((pdir / acc.CLOUD_STATE_FILE).read_text())
    assert state["logins"]["acct_c"]["host_c"]["cloud_ok"] is False
    assert state["last"] == "acct_a"


def test_the_least_loaded_account_is_picked_and_a_login_older_than_the_bound_is_not_used(
    tmp_path,
) -> None:
    pdir = tmp_path / "placement"
    _login(pdir, NOW, ALL_OK)
    picks = [_pick(pdir, NOW, f"r{i}")["account"] for i in range(6)]
    assert sorted(picks) == ["acct_a", "acct_a", "acct_b", "acct_b", "acct_c", "acct_c"]
    acc.release_cloud_slot(pdir, picks[1], "r1")
    assert _pick(pdir, NOW, "r9")["account"] == picks[1]
    later = NOW + timedelta(seconds=FRESH_S + 60)
    stale = _pick(pdir, later, "r10")
    assert stale["account"] is None
    assert stale["needs_probe"] == "host_a,host_b,host_c"


def test_an_account_whose_credit_reads_zero_is_never_used_again(tmp_path) -> None:
    pdir = tmp_path / "placement"
    _login(pdir, NOW, ALL_OK)
    acc.record_cloud_credit(pdir, "acct_a", 0.0, "operator reading", now=NOW)
    got = _pick(pdir, NOW)
    assert got["account"] == "acct_b"
    assert "credit read zero" in got["tried"][0]["skipped"]
    acc.record_cloud_credit(pdir, "acct_b", 0.0, "operator reading", now=NOW)
    acc.record_cloud_credit(pdir, "acct_c", -1.0, "operator reading", now=NOW)
    assert _pick(pdir, NOW)["account"] is None


def test_the_accounts_file_never_waits_on_the_placement_lock(tmp_path) -> None:
    import fcntl

    pdir = tmp_path / "placement"
    pdir.mkdir()
    with (pdir / ".lock").open("a+", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        acc.record_cloud_credit(pdir, "acct_a", 120.0, "operator reading")
        _login(pdir, NOW, ALL_OK)
        got = _pick(pdir, NOW)
        acc.mark_cloud_limited(pdir, "acct_a", "2026-10-25T23:00:00Z")
    assert got["account"] is not None
    assert (pdir / acc.CLOUD_LOCK_FILE).exists()
    assert acc.CLOUD_LOCK_FILE != ".lock"


def test_an_account_runs_up_to_its_cap_and_has_no_daily_bound(tmp_path) -> None:
    pdir = tmp_path / "placement"
    only = ACCOUNTS[:1]
    _login(pdir, NOW, ALL_OK)
    picked = [
        _pick(pdir, NOW, f"r{i}", only)["account"]
        for i in range(acc.CLOUD_CAP_START + 1)
    ]
    assert picked == ["acct_a"] * acc.CLOUD_CAP_START + [None]
    slots = acc.cloud_slots(pdir, only, HOSTS, fresh_s=FRESH_S, now=NOW)
    assert slots["accounts"] == {"acct_a": 0}
    assert (
        "at its cap: 4 running (cap 4)"
        in _pick(pdir, NOW, "r-none", only)["tried"][0]["skipped"]
    )

    for i in range(40):
        acc.record_cloud_run(
            pdir,
            "acct_a",
            {"run_id": f"r{i % acc.CLOUD_CAP_START}", "session_id": f"session_{i}"},
        )
        at = NOW + timedelta(minutes=i)
        _login(pdir, at, ALL_OK)
        assert _pick(pdir, at, f"r{i % acc.CLOUD_CAP_START}", only)["account"]
    assert not hasattr(acc, "CLOUD_STARTS_PER_DAY")

    later = NOW + acc.CLOUD_ACTIVE_TTL + timedelta(hours=1)
    _login(pdir, later, ALL_OK)
    assert acc.cloud_slots(pdir, only, HOSTS, fresh_s=FRESH_S, now=later)[
        "accounts"
    ] == {"acct_a": acc.CLOUD_CAP_START}


def test_the_cap_rises_while_starts_succeed_at_it_and_falls_to_the_running_count_on_a_refusal(
    tmp_path,
) -> None:
    pdir = tmp_path / "placement"
    only = ACCOUNTS[:1]
    now = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
    _login(pdir, now, ALL_OK)

    def free() -> dict[str, int]:
        return acc.cloud_slots(pdir, only, HOSTS, fresh_s=FRESH_S, now=now)["accounts"]

    for i in range(acc.CLOUD_CAP_START):
        _pick(pdir, now, f"s{i}", only)
        cap = acc.note_cloud_start(pdir, "acct_a", now=now)
    assert cap == acc.CLOUD_CAP_START + 1
    assert free() == {"acct_a": 1}
    _pick(pdir, now, "s-refused", only)
    assert acc.cloud_start_refused(
        "Error: too many concurrent sessions for this account"
    )
    assert not acc.cloud_start_refused(
        "Quick safety check: Is this a project you trust?"
    )
    assert (
        acc.note_cloud_refusal(pdir, "acct_a", "s-refused", "too many", now=now)
        == acc.CLOUD_CAP_START
    )
    assert free() == {"acct_a": 0}

    acc.record_cloud_run(pdir, "acct_a", {"run_id": "s0", "session_id": "session_0"})
    _pick(pdir, now, "s4", only)
    assert (
        acc.note_cloud_start(pdir, "acct_a", now=now + timedelta(minutes=5))
        == acc.CLOUD_CAP_START
    )
    assert acc.note_cloud_start(
        pdir, "acct_a", now=now + acc.CLOUD_REFUSAL_HOLD + timedelta(minutes=1)
    ) == (acc.CLOUD_CAP_START + 1)


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
