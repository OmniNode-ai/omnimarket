# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cloud capacity is counted per claude.ai login (OMN-22243): the registry, the login map, the transports, the pick by
account then transport, and the one-time import of the host-keyed state file."""

import hashlib
import json
import shutil
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
    ModelCloudAccount,
    ModelCloudSessionPlacementRequest,
    ModelCloudSessionPlacementResult,
)

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"
FRESH_S = 1800
PROBE_OK = "Error: Invalid session ID"
PROBE_BAD = "Please sign-in to continue"
SESSION = json.dumps({"ok": True, "session_id": "session_abcdefgh12"})


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:10]


def _account(
    label: str = "lab-primary", org: str = "org-1", **over: Any
) -> ModelCloudAccount:
    fields: dict[str, Any] = {
        "label": label,
        "org_hash": _h(org),
        "email_hash": _h(f"{label}@example.test"),
        "consent": "2026-11-02T02:30:18Z",
        "legacy_hosts": (),
        "transport_exclude": (),
    }
    return ModelCloudAccount(**{**fields, **over})


def _obs(
    host: str, account: ModelCloudAccount | None, probe: str = PROBE_OK, **over: Any
) -> dict[str, Any]:
    """One host's raw texts; ``account`` None is a logged-out host."""
    if account is None:
        auth = json.dumps({"loggedIn": False})
    else:
        org = {"lab-primary": "org-1"}.get(account.label, "org-x")
        auth = json.dumps(
            {"loggedIn": True, "orgId": org, "email": f"{account.label}@example.test"}
        )
    return {"host": host, "auth_text": auth, "probe_text": probe, **over}


def _call(
    tmp: Path, action: str, now: datetime = NOW, **fields: Any
) -> ModelCloudSessionPlacementResult:
    fields.setdefault("login_fresh_s", FRESH_S)
    return HandlerCloudSessionPlacement().handle(
        ModelCloudSessionPlacementRequest(
            action=action, placement_dir=str(tmp), now=now, **fields
        )
    )


def _hosts(*names: str) -> tuple[dict[str, Any], ...]:
    return tuple({"name": n, "target": f"{n}.example.test"} for n in names)


def _ready(
    tmp: Path,
    accounts: tuple[ModelCloudAccount, ...],
    seen: dict[str, ModelCloudAccount | None],
    **kw: Any,
):
    """Observe ``seen`` (host -> account) and return the transports in table order."""
    got = _call(
        tmp,
        "observe_logins",
        accounts=accounts,
        observations=[_obs(h, a, **kw) for h, a in seen.items()],
    )
    assert got.error is None, got.error
    return _hosts(*seen)


def _state(tmp: Path) -> dict[str, Any]:
    return json.loads((tmp / acc.CLOUD_STATE_FILE).read_text())


# AC1 ---------------------------------------------------------------------------------------------------------


def test_one_account_two_hosts_one_hash_pair_one_cap_summed_running(
    tmp_path: Path,
) -> None:
    a = _account()
    both = _ready(tmp_path, (a,), {"h200": a, "h201": a})
    first = _call(tmp_path, "pick", accounts=(a,), transports=both, run_id="r1")
    assert (first.account, first.transport) == ("lab-primary", "h200")
    _ready(
        tmp_path, (a,), {"h200": None}
    )  # h200 logs out: the second run goes through h201
    second = _call(tmp_path, "pick", accounts=(a,), transports=both, run_id="r2")
    assert (second.account, second.transport) == ("lab-primary", "h201")
    slots = _call(tmp_path, "slots", accounts=(a,), transports=both)
    assert list(slots.accounts) == ["lab-primary"]
    assert slots.caps["lab-primary"]["running"] == 2
    assert slots.accounts["lab-primary"] == slots.caps["lab-primary"]["cap"] - 2


def test_unavailable_host_consented_account_is_picked(tmp_path: Path) -> None:
    a = _account()
    rows = (
        {"name": "h200", "target": "h200.example.test", "available": False},
        {"name": "h201", "target": "h201.example.test", "available": False},
    )
    got = _call(tmp_path, "transports", accounts=(a,), rows=rows, probe_timeout_s=120)
    assert [t["name"] for t in got.transports] == ["h200", "h201"]
    _ready(tmp_path, (a,), {"h201": a})
    pick = _call(
        tmp_path, "pick", accounts=(a,), transports=tuple(got.transports), run_id="r1"
    )
    assert (pick.account, pick.transport) == ("lab-primary", "h201")


# AC2 ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("email_of_other", ["same-org-other-email", "other-org"])
def test_unconsented_login_is_never_picked_and_is_listed_with_host_and_hash(
    tmp_path: Path, email_of_other: str
) -> None:
    a = _account()
    org = "org-1" if email_of_other == "same-org-other-email" else "org-9"
    stranger = {
        "host": "h051",
        "auth_text": json.dumps(
            {"loggedIn": True, "orgId": org, "email": "stranger@example.test"}
        ),
        "probe_text": PROBE_OK,
    }
    got = _call(tmp_path, "observe_logins", accounts=(a,), observations=[stranger])
    assert got.unconsented[0]["host"] == "h051"
    assert got.unconsented[0]["org_hash"] == _h(org)
    assert got.unconsented[0]["email_hash"] == _h("stranger@example.test")
    assert "stranger@example.test" not in json.dumps(got.model_dump())
    pick = _call(
        tmp_path, "pick", accounts=(a,), transports=_hosts("h051"), run_id="r1"
    )
    assert pick.account is None
    assert pick.transport is None
    assert any(u["host"] == "h051" for u in pick.unconsented)


@pytest.mark.parametrize(
    "later",
    [
        {"auth_text": json.dumps({"loggedIn": False})},
        {
            "auth_text": json.dumps(
                {"loggedIn": True, "orgId": "org-1", "email": "changed@example.test"}
            )
        },
        {"probe_text": PROBE_BAD},
        {"reachable": False, "auth_text": "", "probe_text": ""},
    ],
    ids=["logged-out", "changed-identity", "failed-probe", "unreachable"],
)
def test_transition_host_leaves_its_old_account_at_the_next_observation(
    tmp_path: Path, later: dict[str, Any]
) -> None:
    a = _account()
    t = _ready(tmp_path, (a,), {"h200": a})
    assert _call(tmp_path, "pick", accounts=(a,), transports=t).account == "lab-primary"
    got = _call(
        tmp_path,
        "observe_logins",
        accounts=(a,),
        observations=[{**_obs("h200", a), **later}],
        now=NOW + timedelta(seconds=60),
    )
    assert got.error is None
    pick = _call(
        tmp_path, "pick", accounts=(a,), transports=t, now=NOW + timedelta(seconds=61)
    )
    assert pick.account is None
    assert pick.transport is None
    assert pick.sentences
    assert all("CLOUD-ACCOUNT lab-primary skipped:" in s for s in pick.sentences)


# AC3 ---------------------------------------------------------------------------------------------------------


def _setup_skip(tmp: Path, kind: str) -> tuple[tuple[ModelCloudAccount, ...], str]:
    over: dict[str, Any] = {}
    if kind == "consent_expires":
        over["consent_expires"] = "2026-11-03T12:00:00Z"
    if kind == "credit_expires":
        over["credit_expires"] = "2026-11-02"
    a = _account(**over)
    _ready(tmp, (a,), {"h200": a})
    if kind == "hold":
        _call(
            tmp,
            "mark_limited",
            accounts=(a,),
            account="lab-primary",
            until="2026-11-05T08:00:00Z",
            why="weekly limit",
        )
    if kind == "zero_credit":
        _call(
            tmp,
            "record_credit",
            account="lab-primary",
            remaining=0,
            source="operator reading",
        )
    if kind == "full_cap":
        for i in range(acc.CLOUD_CAP_START):
            assert _call(
                tmp, "pick", accounts=(a,), transports=_hosts("h200"), run_id=f"r{i}"
            ).account
    date = {
        "hold": "2026-11-05T08:00:00Z",
        "consent_expires": "2026-11-03T12:00:00Z",
        "credit_expires": "2026-11-02",
        "zero_credit": "2026-11-03T12:00:00Z",
        "full_cap": "2026-11-03T16:00:00Z",
    }[kind]
    return (a,), date


@pytest.mark.parametrize(
    "kind", ["hold", "consent_expires", "credit_expires", "zero_credit", "full_cap"]
)
def test_skip_sentence_names_cause_and_date_and_leaves_the_account_out(
    tmp_path: Path, kind: str
) -> None:
    accounts, date = _setup_skip(tmp_path, kind)
    pick = _call(
        tmp_path, "pick", accounts=accounts, transports=_hosts("h200"), run_id="rx"
    )
    assert pick.account is None
    assert len(pick.sentences) == 1
    sentence = pick.sentences[0]
    assert sentence.startswith("CLOUD-ACCOUNT lab-primary skipped: ")
    assert date in sentence
    assert "no account" not in sentence.lower()


def test_skip_sentence_consent_one_second_before_expiry_is_picked(
    tmp_path: Path,
) -> None:
    a = _account(consent_expires="2026-11-03T12:00:00Z")
    t = _ready(tmp_path, (a,), {"h200": a})
    before = _call(
        tmp_path, "pick", accounts=(a,), transports=t, now=NOW - timedelta(seconds=1)
    )
    assert before.account == "lab-primary"


def test_skip_sentence_empty_registry_never_says_no_account(tmp_path: Path) -> None:
    pick = _call(tmp_path, "pick", accounts=(), transports=_hosts("h200"))
    assert pick.account is None
    assert pick.sentences == ["the registry names no cloud account"]


def test_skip_sentence_credit_date_is_inclusive(tmp_path: Path) -> None:
    a = _account(credit_expires="2026-11-03")
    t = _ready(tmp_path, (a,), {"h200": a})
    assert _call(tmp_path, "pick", accounts=(a,), transports=t).account == "lab-primary"


def test_skip_sentence_stale_login_asks_for_a_new_observation(tmp_path: Path) -> None:
    a = _account()
    t = _ready(tmp_path, (a,), {"h200": a})
    later = NOW + timedelta(seconds=FRESH_S + 1)
    pick = _call(tmp_path, "pick", accounts=(a,), transports=t, now=later)
    assert pick.account is None
    assert pick.needs_probe == "h200"
    assert pick.stale_hosts == ["h200"]
    assert "older than 1800 seconds" in pick.sentences[0]


# AC4 ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "excluded_by"),
    [
        ({"name": "h105", "target": "h105.example.test"}, "transport_exclude"),
        ({"name": "h203", "target": "h203.example.test", "push": False}, "push: false"),
        (
            {"name": "h204", "target": "h204.example.test", "contributor": True},
            "contributor",
        ),
    ],
    ids=["transport_exclude", "push_false", "contributor_true"],
)
def test_transports_drops_the_row_before_any_probe_is_requested(
    tmp_path: Path, row: dict[str, Any], excluded_by: str
) -> None:
    a = _account(transport_exclude=("h105",))
    got = _call(tmp_path, "transports", accounts=(a,), rows=(row,), probe_timeout_s=120)
    probes_requested = [
        t["name"] for t in got.transports
    ]  # the caller probes exactly these
    assert probes_requested == []
    assert got.excluded == [
        f"CLOUD-HOST {row['name']} excluded: " + got.excluded[0].split(": ", 1)[1]
    ]
    assert excluded_by in got.excluded[0]
    pick = _call(
        tmp_path, "pick", accounts=(a,), transports=tuple(got.transports), run_id="r1"
    )
    assert pick.account is None


def test_transports_keeps_table_order_and_returns_the_probe_timeout(
    tmp_path: Path,
) -> None:
    rows = (*_hosts("h201", "h200"), {"name": "local", "local": True})
    got = _call(
        tmp_path, "transports", accounts=(_account(),), rows=rows, probe_timeout_s=120
    )
    assert [t["name"] for t in got.transports] == ["h201", "h200", "local"]
    assert got.probe_timeout_s == 120


# AC5 ---------------------------------------------------------------------------------------------------------


def _legacy_file(tmp: Path, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    shutil.copy(
        FIXTURES / "cloud_accounts_h200_2026-11-02.json",
        tmp / acc.CLOUD_LEGACY_STATE_FILE,
    )
    data = json.loads((tmp / acc.CLOUD_LEGACY_STATE_FILE).read_text())
    data["accounts"]["h201"] = {
        "cap": 7,
        "credit": {
            "read_at": "2026-11-03T00:30:00Z",
            "remaining": 198.5,
            "source": "operator reading",
        },
        "active": {
            "run_old": "2026-11-03T11:00:00Z",
            "run_dead": "2026-11-01T11:00:00Z",
        },
        "login": "ok",
    }
    data["accounts"].update(extra or {})
    (tmp / acc.CLOUD_LEGACY_STATE_FILE).write_text(json.dumps(data))
    return data


def test_migration_keeps_newest_credit_restores_rejected_rate_hold_and_renames_file(
    tmp_path: Path,
) -> None:
    a = _account(legacy_hosts=("h200", "h201"))
    _legacy_file(tmp_path)
    got = _call(tmp_path, "slots", accounts=(a,))
    assert got.error is None, got.error
    state = _state(tmp_path)
    entry = state["accounts"]["lab-primary"]
    assert (
        entry["credit"]["remaining"] == 198.5
    )  # h201's reading is newer than h200's 244.0
    # Migration retains telemetry without turning it into a refusal.
    assert "limited_until" not in entry
    assert entry["rate"]["status"] == "rejected"
    assert acc.cloud_account_skip(entry, NOW) is None
    assert entry["cap"] == 7  # the lower cap
    assert list(entry["active"]) == ["run_old"]  # union of unexpired active runs
    assert state["last"] == "lab-primary"
    assert not (tmp_path / acc.CLOUD_LEGACY_STATE_FILE).exists()
    assert (tmp_path / acc.CLOUD_LEGACY_IMPORTED).exists()
    assert set(state["accounts"]) == {"lab-primary"}


def test_migration_keeps_the_latest_recorded_hold(tmp_path: Path) -> None:
    a = _account(legacy_hosts=("h200", "h201"))
    data = _legacy_file(tmp_path)
    data["accounts"]["h201"].update(
        limited_until="2026-11-09T00:00:00Z",
        limited_source="recorded",
        limited_why="weekly limit",
    )
    (tmp_path / acc.CLOUD_LEGACY_STATE_FILE).write_text(json.dumps(data))
    _call(tmp_path, "slots", accounts=(a,))
    entry = _state(tmp_path)["accounts"]["lab-primary"]
    assert (entry["limited_until"], entry["limited_source"]) == (
        "2026-11-09T00:00:00Z",
        "recorded",
    )


def test_migration_refuses_a_recorded_hold_on_a_host_outside_legacy_hosts_by_name(
    tmp_path: Path,
) -> None:
    a = _account(legacy_hosts=("h200", "h201"))
    _legacy_file(
        tmp_path,
        {
            "h101": {
                "limited_until": "2026-11-09T00:00:00Z",
                "limited_source": "recorded",
                "limited_why": "weekly limit",
            }
        },
    )
    got = _call(tmp_path, "slots", accounts=(a,))
    assert got.error is not None
    assert "h101" in got.error
    assert (tmp_path / acc.CLOUD_LEGACY_STATE_FILE).exists()
    assert not (tmp_path / acc.CLOUD_STATE_FILE).exists()


def test_migration_drops_quiet_entries_of_other_hosts(tmp_path: Path) -> None:
    a = _account(legacy_hosts=("h200", "h201"))
    _legacy_file(tmp_path, {"h101": {"cap": 3, "login": "no-claude-ai-login"}})
    assert _call(tmp_path, "slots", accounts=(a,)).error is None
    assert "h101" not in _state(tmp_path)["accounts"]


def test_migration_is_refused_for_a_call_that_has_no_registry(tmp_path: Path) -> None:
    _legacy_file(tmp_path)
    got = _call(tmp_path, "credit", account="lab-primary")
    assert got.error is not None
    assert "one-time import" in got.error
    assert (tmp_path / acc.CLOUD_LEGACY_STATE_FILE).exists()


def test_duplicate_label_and_duplicate_pair_are_refused_by_name(tmp_path: Path) -> None:
    a = _account()
    twin_label = _account(org="org-2")
    same_pair = _account(label="lab-second").model_copy(
        update={"org_hash": a.org_hash, "email_hash": a.email_hash}
    )
    by_label = _call(tmp_path, "slots", accounts=(a, twin_label))
    assert by_label.error is not None
    assert "duplicate account label 'lab-primary'" in by_label.error
    by_pair = _call(tmp_path, "slots", accounts=(a, same_pair))
    assert by_pair.error is not None
    assert "'lab-primary'" in by_pair.error
    assert "'lab-second'" in by_pair.error
    assert "hash pair" in by_pair.error


def test_duplicate_registry_absent_key_is_refused_by_name(tmp_path: Path) -> None:
    got = _call(
        tmp_path, "slots", accounts=(ModelCloudAccount(label="x", org_hash=_h("o")),)
    )
    assert got.error == "accounts[0] 'x' lacks email_hash"
    assert (
        _call(tmp_path, "slots", accounts=(_account(),), login_fresh_s=None).error
        == "missing field: login_fresh_s"
    )


def test_duplicate_absent_credit_does_not_gate_and_prints_a_flag(
    tmp_path: Path,
) -> None:
    a = _account()
    t = _ready(tmp_path, (a,), {"h200": a})
    pick = _call(tmp_path, "pick", accounts=(a,), transports=t)
    assert pick.account == "lab-primary"
    assert pick.flags
    assert "no credit reading" in pick.flags[0]


# AC6 ---------------------------------------------------------------------------------------------------------


class FakeSsh:
    """The injected ssh seam: transport -> (the start call began, its raw answer). Records every call."""

    def __init__(self, answers: dict[str, tuple[bool, str]]) -> None:
        self.answers, self.calls = answers, []

    def __call__(self, transport: str) -> tuple[bool, str]:
        self.calls.append(transport)
        return self.answers[transport]


def _picked(tmp: Path, run: str) -> tuple[tuple[ModelCloudAccount, ...], list[str]]:
    a = _account()
    t = _ready(tmp, (a,), {"h200": a, "h201": a})
    pick = _call(tmp, "pick", accounts=(a,), transports=t, run_id=run)
    return (a,), [x["name"] for x in pick.transports]


def test_start_failure_before_the_call_tries_the_next_transport_and_leaves_the_account_unheld(
    tmp_path: Path,
) -> None:
    _, order = _picked(tmp_path, "r1")
    assert order == ["h200", "h201"]
    ssh = FakeSsh({"h200": (False, "ssh: connect refused"), "h201": (True, SESSION)})
    out = acc.run_start(
        tmp_path, "lab-primary", "r1", order, ssh, "https://example.test/code/", now=NOW
    )
    assert ssh.calls == ["h200", "h201"]
    assert out["verdict"] == "started"
    assert out["transport"] == "h201"
    assert "limited_until" not in _state(tmp_path)["accounts"]["lab-primary"]


def test_start_failure_after_the_call_began_is_start_unknown_with_one_start_call_and_the_slot_kept(
    tmp_path: Path,
) -> None:
    accounts, order = _picked(tmp_path, "r1")
    ssh = FakeSsh(
        {
            "h200": (True, "connection reset while reading the answer"),
            "h201": (True, SESSION),
        }
    )
    out = acc.run_start(
        tmp_path, "lab-primary", "r1", order, ssh, "https://example.test/code/", now=NOW
    )
    assert ssh.calls == [
        "h200"
    ]  # exactly one start call: no second transport after the call began
    assert out["verdict"] == "start-unknown"
    assert out["sentence"] == "start-unknown r1 unresolved"
    assert (
        _call(tmp_path, "slots", accounts=accounts).caps["lab-primary"]["running"] == 1
    )


def test_start_with_no_reachable_transport_fails_and_releases_the_slot(
    tmp_path: Path,
) -> None:
    accounts, order = _picked(tmp_path, "r1")
    ssh = FakeSsh({"h200": (False, "down"), "h201": (False, "down")})
    out = acc.run_start(
        tmp_path, "lab-primary", "r1", order, ssh, "https://example.test/code/", now=NOW
    )
    assert out["verdict"] == "failed"
    assert ssh.calls == ["h200", "h201"]
    assert (
        _call(tmp_path, "slots", accounts=accounts).caps["lab-primary"]["running"] == 0
    )


def test_start_refusal_text_holds_the_account(tmp_path: Path) -> None:
    accounts, order = _picked(tmp_path, "r1")
    ssh = FakeSsh({"h200": (True, "weekly limit reached, resets 2026-11-04T00:00:00Z")})
    out = acc.run_start(
        tmp_path, "lab-primary", "r1", order, ssh, "https://example.test/code/", now=NOW
    )
    assert out["verdict"] == "refused"
    assert ssh.calls == ["h200"]
    pick = _call(
        tmp_path,
        "pick",
        accounts=accounts,
        transports=_hosts("h200", "h201"),
        run_id="r2",
    )
    assert pick.account is None
    assert "2026-11-04T00:00:00Z" in pick.sentences[0]


def test_start_outcome_action_matches_the_library(tmp_path: Path) -> None:
    _picked(tmp_path, "r1")
    accounts = (_account(),)
    nxt = _call(
        tmp_path,
        "start_outcome",
        accounts=accounts,
        account="lab-primary",
        run_id="r1",
        transport="h200",
        untried=("h201",),
    )
    assert (nxt.verdict, nxt.next_transport) == ("try-next", "h201")
    done = _call(
        tmp_path,
        "start_outcome",
        accounts=accounts,
        account="lab-primary",
        run_id="r1",
        transport="h201",
        began=True,
        text=SESSION,
        session_url_base="https://example.test/code/",
    )
    assert done.verdict == "started"
    assert done.parsed is not None
    assert done.parsed["session_id"] == "session_abcdefgh12"


def test_observe_logins_rerun_with_the_same_texts_changes_only_seen_at(
    tmp_path: Path,
) -> None:
    a = _account()
    stranger = {
        "host": "h051",
        "auth_text": json.dumps(
            {"loggedIn": True, "orgId": "o9", "email": "s@example.test"}
        ),
        "probe_text": PROBE_OK,
    }
    obs = [_obs("h200", a), _obs("h201", a, PROBE_BAD), stranger]
    _call(tmp_path, "observe_logins", accounts=(a,), observations=obs)
    first = _state(tmp_path)
    _call(
        tmp_path,
        "observe_logins",
        accounts=(a,),
        observations=obs,
        now=NOW + timedelta(seconds=300),
    )
    second = _state(tmp_path)
    assert second != first  # seen_at moved

    def normalized(state: dict[str, Any]) -> str:
        return (
            json.dumps(state, sort_keys=True)
            .replace("2026-11-03T12:05:00Z", "T")
            .replace("2026-11-03T12:00:00Z", "T")
        )

    assert normalized(second) == normalized(first)
