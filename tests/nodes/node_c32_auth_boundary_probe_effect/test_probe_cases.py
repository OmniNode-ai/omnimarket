# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""C32's five auth cases and the planted-credential search, pinned offline (OMN-19984; moved into omnimarket by OMN-23213).

Every grader here is driven by a FAKE SERVER: a function with the probe's own
`request(url, headers) -> Answer` shape. Three of them matter:

* `ideal_server` -- loopback plus a per-start token traded for a cookie, a wrong
  Host refused. Every case must hold and the verdict must be PASS.
* `tokenless_server` -- what `onex dashboard` serves today (omnimarket 0.4.307,
  read on lab host h101 2026-10-09): no token in the start URL, every loopback
  read answered 200 whatever the Host header, nothing on the LAN address. Cases
  1, 2, 3 and 5 must FAIL, case 4 must hold.
* `dead_server` -- nothing answers. Every case must FAIL: a refusal is a 401 or
  403 the server chose to send, never the silence of a process that is not there.

No test starts a process: the autouse guard replaces the one starter the probes
use and fails any test that reached it.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

import pytest

from omnimarket.nodes.node_c32_auth_boundary_probe_effect.handlers import (
    probe_base as base,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.handlers import (
    probe_c32 as c32,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.handlers.probe_base import (
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_PRECONDITION_ABSENT,
    Clause,
    Precondition,
)

pytestmark = pytest.mark.unit

PORT = 38732
BASE = f"http://127.0.0.1:{PORT}"
LAN = "192.0.2.10"
CID = base.FIXTURE_CORRELATION_ID
STALE_TOKEN = "t-first-start"
CURRENT_TOKEN = "t-second-start"
PRESENT = Precondition("p", True, "d", "OMN-1")
ABSENT = Precondition("q", False, "d", "OMN-2")


@pytest.fixture(autouse=True)
def no_process_starts(monkeypatch):
    started = []

    def refuse(argv, **_kwargs):
        started.append(list(argv))
        raise OSError("unit tests never start a process")

    monkeypatch.setattr(base, "_popen", refuse)
    yield started
    assert not started, f"a unit test reached the process starter: {started}"


# ------------------------------------------------------------ fake servers


def _split(url: str, headers: dict) -> tuple[str, str, dict, str]:
    parts = urllib.parse.urlsplit(url)
    host = headers.get("Host", parts.netloc)
    return (
        parts.hostname or "",
        host,
        dict(urllib.parse.parse_qsl(parts.query)),
        parts.path,
    )


def _body() -> str:
    return json.dumps(
        {"rows": [{"correlation_id": CID, "model_name": "probe-fixture"}]}
    )


def ideal_server(token: str = CURRENT_TOKEN, *, allow_origin: str | None = None):
    def request(url: str, headers: dict) -> c32.Answer:
        hostname, host, query, _path = _split(url, headers)
        if hostname != "127.0.0.1":
            return c32.Answer(None)
        if host != f"127.0.0.1:{PORT}":
            return c32.Answer(421, body='{"error":"host_not_allowed"}')
        cookie = headers.get("Cookie", "")
        bearer = headers.get("Authorization", "")
        if query.get("token") == token:
            return c32.Answer(200, allow_origin, _body(), (f"onex_dashboard={token}",))
        if f"onex_dashboard={token}" in cookie or bearer == f"Bearer {token}":
            return c32.Answer(200, allow_origin, _body())
        return c32.Answer(401, body='{"error":"token_required"}')

    return request


def tokenless_server(url: str, headers: dict) -> c32.Answer:
    hostname, _host, _query, _path = _split(url, headers)
    if hostname != "127.0.0.1":
        return c32.Answer(None)
    return c32.Answer(200, None, _body())


def dead_server(url: str, headers: dict) -> c32.Answer:
    return c32.Answer(None)


def observe(
    request, *, current: c32.Credential, stale: c32.Credential, lan: str | None = LAN
):
    return c32.observe(
        BASE, current, stale, request=request, lan_address=lan, correlation_id=CID
    )


def ideal_credentials(request=None):
    request = request or ideal_server()
    current = c32.credential_from_start(f"{BASE}/?token={CURRENT_TOKEN}", request)
    stale = c32.credential_from_start(
        f"{BASE}/?token={STALE_TOKEN}", ideal_server(STALE_TOKEN)
    )
    return current, stale


def grade(obs) -> dict[str, Clause]:
    return {c.name: c for c in c32.grade_auth_cases(obs, correlation_id=CID)}


CASE = {
    key: name
    for key, (name, _served) in zip(
        ("served", "stale", "none", "interface", "origin"), c32.AUTH_CASES, strict=True
    )
}


# ------------------------------------------------------- the credential


def test_the_start_url_token_travels_three_ways_and_picks_up_the_cookie() -> None:
    cred = c32.credential_from_start(f"{BASE}/?token={CURRENT_TOKEN}", ideal_server())
    assert cred.present
    assert cred.query == f"token={CURRENT_TOKEN}"
    assert cred.bearer == CURRENT_TOKEN
    assert cred.cookies == (f"onex_dashboard={CURRENT_TOKEN}",)


def test_a_fragment_token_is_read_too() -> None:
    cred = c32.credential_from_start(f"{BASE}/#token={CURRENT_TOKEN}", dead_server)
    assert cred.present
    assert cred.bearer == CURRENT_TOKEN
    assert cred.query == ""


def test_a_start_url_with_nothing_in_it_mints_nothing() -> None:
    cred = c32.credential_from_start(BASE, tokenless_server)
    assert not cred.present


def test_a_missing_start_url_is_an_empty_credential() -> None:
    assert not c32.credential_from_start(None, ideal_server()).present


def test_headers_never_carry_an_absent_credential() -> None:
    assert c32.Credential().headers() == {}
    assert c32.Credential().url(f"{BASE}/projections") == f"{BASE}/projections"
    assert (
        c32.Credential(query="token=x").url(f"{BASE}/p?a=1") == f"{BASE}/p?a=1&token=x"
    )


# ---------------------------------------------------------- the five cases


def test_an_ideal_server_holds_every_case() -> None:
    current, stale = ideal_credentials()
    clauses = grade(observe(ideal_server(), current=current, stale=stale))
    assert [c.name for c in clauses.values()] == [name for name, _ in c32.AUTH_CASES]
    assert all(c.ok for c in clauses.values()), [
        c for c in clauses.values() if not c.ok
    ]


def test_todays_tokenless_dashboard_fails_cases_one_two_three_and_five() -> None:
    """The live product on 2026-10-09: the four FAILs are its defect, not the probe's."""
    current = c32.credential_from_start(BASE, tokenless_server)
    stale = c32.credential_from_start(BASE, tokenless_server)
    clauses = grade(observe(tokenless_server, current=current, stale=stale))
    assert not clauses[CASE["served"]].ok
    assert "no token" in clauses[CASE["served"]].detail
    assert not clauses[CASE["stale"]].ok
    assert not clauses[CASE["none"]].ok
    assert clauses[CASE["interface"]].ok
    assert not clauses[CASE["origin"]].ok
    assert "rebinding" in clauses[CASE["origin"]].detail


def test_a_dead_server_fails_every_case_silence_is_not_a_refusal() -> None:
    current, stale = ideal_credentials()
    clauses = grade(observe(dead_server, current=current, stale=stale))
    for key in ("served", "stale", "none", "origin"):
        assert not clauses[CASE[key]].ok, key


def test_a_server_that_refuses_everyone_fails_the_positive_control() -> None:
    current, stale = ideal_credentials()
    clauses = grade(
        observe(ideal_server("a-token-nobody-has"), current=current, stale=stale)
    )
    assert not clauses[CASE["served"]].ok
    # The refusals still read as refusals, but no verdict can pass without case 1.
    assert clauses[CASE["none"]].ok


def test_a_served_answer_without_the_fixture_row_is_not_the_positive_control() -> None:
    def empty_rows(url, headers):
        answer = ideal_server()(url, headers)
        return c32.Answer(
            answer.status, answer.allow_origin, '{"rows": []}', answer.set_cookies
        )

    current, stale = ideal_credentials(empty_rows)
    clauses = grade(observe(empty_rows, current=current, stale=stale))
    assert not clauses[CASE["served"]].ok
    assert CID in clauses[CASE["served"]].detail


def test_a_token_that_does_not_change_between_starts_is_not_per_start() -> None:
    current, _ = ideal_credentials()
    clauses = grade(observe(ideal_server(), current=current, stale=current))
    assert not clauses[CASE["stale"]].ok
    assert "did not change" in clauses[CASE["stale"]].detail


def test_a_stale_token_still_served_fails() -> None:
    def accepts_both(url, headers):
        answer = ideal_server()(url, headers)
        if answer.status == 401:
            return ideal_server(STALE_TOKEN)(url, headers)
        return answer

    current, stale = ideal_credentials()
    assert not grade(observe(accepts_both, current=current, stale=stale))[
        CASE["stale"]
    ].ok


def test_a_404_is_not_a_refusal() -> None:
    def not_found_without_token(url, headers):
        answer = ideal_server()(url, headers)
        return c32.Answer(404) if answer.status == 401 else answer

    current, stale = ideal_credentials()
    clauses = grade(observe(not_found_without_token, current=current, stale=stale))
    assert not clauses[CASE["none"]].ok
    assert not clauses[CASE["stale"]].ok


def test_a_lan_answer_fails_the_interface_case() -> None:
    def lan_open(url, headers):
        if urllib.parse.urlsplit(url).hostname == LAN:
            return c32.Answer(200, None, _body())
        return ideal_server()(url, headers)

    current, stale = ideal_credentials()
    assert not grade(observe(lan_open, current=current, stale=stale))[
        CASE["interface"]
    ].ok


def test_no_lan_address_cannot_pass_the_interface_case() -> None:
    current, stale = ideal_credentials()
    clause = grade(observe(ideal_server(), current=current, stale=stale, lan=None))[
        CASE["interface"]
    ]
    assert not clause.ok
    assert "non-loopback" in clause.detail


def test_a_loopback_address_is_not_a_non_loopback_interface() -> None:
    current, stale = ideal_credentials()
    clause = grade(
        observe(ideal_server(), current=current, stale=stale, lan="127.0.0.2")
    )[CASE["interface"]]
    assert not clause.ok


@pytest.mark.parametrize("allow_origin", ["*", "http://c32-cross-origin.invalid"])
def test_a_permissive_cors_answer_fails_the_cross_origin_case(
    allow_origin: str,
) -> None:
    server = ideal_server(allow_origin=allow_origin)
    current, stale = ideal_credentials(server)
    assert not grade(observe(server, current=current, stale=stale))[CASE["origin"]].ok


def test_a_rebinding_host_served_fails_the_cross_origin_case() -> None:
    def ignores_host(url, headers):
        return ideal_server()(url, {k: v for k, v in headers.items() if k != "Host"})

    current, stale = ideal_credentials(ignores_host)
    clauses = grade(observe(ignores_host, current=current, stale=stale))
    # The rebinding request carries no credential, so a token server refuses it anyway.
    assert clauses[CASE["origin"]].ok
    clauses = grade(observe(tokenless_server, current=current, stale=stale))
    assert not clauses[CASE["origin"]].ok


def test_every_request_the_cases_make_is_recorded_for_the_secret_search() -> None:
    current, stale = ideal_credentials()
    obs = observe(ideal_server(), current=current, stale=stale)
    assert len(obs.bodies()) >= 2 * 6


# --------------------------------------------------- the planted credential


VALUE = "sk-or-v1-c32planted-0123456789abcdef"


def findings(**over) -> c32.SecretFindings:
    facts = {
        "control_found": True,
        "in_responses": (),
        "in_logs": (),
        "in_tables": (),
        "responses_read": 14,
        "files_read": 3,
        "tables_read": 12,
    }
    facts.update(over)
    return c32.SecretFindings(**facts)


def test_a_clean_search_with_a_working_control_holds() -> None:
    assert c32.grade_planted_secret(findings()).ok


@pytest.mark.parametrize(
    "over",
    [
        {
            "in_responses": (
                "/projection/onex.snapshot.projection.tenant-credentials.v1",
            )
        },
        {"in_logs": ("logs/dashboard-2.log",)},
        {"in_tables": ("delegation_events",)},
    ],
)
def test_planted_negative_the_value_anywhere_it_must_not_be_fails(over: dict) -> None:
    clause = c32.grade_planted_secret(findings(**over))
    assert not clause.ok
    assert next(iter(over.values()))[0] in clause.detail


def test_a_search_that_cannot_find_the_value_in_the_raw_store_proves_nothing() -> None:
    clause = c32.grade_planted_secret(findings(control_found=False))
    assert not clause.ok
    assert "positive control" in clause.detail


@pytest.mark.parametrize(
    "over", [{"responses_read": 0}, {"tables_read": 0}, {"files_read": 0}]
)
def test_a_search_over_nothing_cannot_pass(over: dict) -> None:
    assert not c32.grade_planted_secret(findings(**over)).ok


def test_scan_files_finds_the_value_and_skips_the_store_family(tmp_path: Path) -> None:
    (tmp_path / "home" / ".omninode" / "delegation").mkdir(parents=True)
    store = tmp_path / "home" / ".omninode" / "delegation" / "delegation.sqlite"
    store.write_bytes(b"xx" + VALUE.encode() + b"yy")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "clean.log").write_text("nothing here")
    (tmp_path / "home" / "events.jsonl").write_text(json.dumps({"v": VALUE}))
    hits, read = c32.scan_files(tmp_path, VALUE, exclude=c32.store_family(store))
    assert hits == ("home/events.jsonl",)
    assert read == 2
    assert c32.raw_store_holds(store, VALUE)
    assert not c32.raw_store_holds(store, "something-else")


def test_scan_tables_skips_only_the_credential_table(tmp_path: Path) -> None:
    db = tmp_path / "delegation.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE creds (ref TEXT, value TEXT)")
    conn.execute("CREATE TABLE events (payload TEXT)")
    conn.execute("CREATE TABLE clean (payload BLOB)")
    conn.execute("INSERT INTO creds VALUES ('llm.openrouter.api_key', ?)", (VALUE,))
    conn.execute("INSERT INTO events VALUES (?)", (f'{{"k":"{VALUE}"}}',))
    conn.execute("INSERT INTO clean VALUES (?)", (b"nothing",))
    conn.commit()
    conn.close()
    hits, read = c32.scan_tables(db, VALUE, credential_table="creds")
    assert hits == ("events",)
    assert read == 2


def test_the_record_and_the_render_never_carry_the_planted_value(monkeypatch) -> None:
    seen = {}

    def auth_check(env, scratch, value):
        seen["value"] = value
        clauses = [Clause(name, True, "ok") for name, _ in c32.AUTH_CASES]
        clauses.append(c32.grade_planted_secret(findings()))
        return [PRESENT], clauses

    monkeypatch.setattr(c32, "onex_cli", lambda: PRESENT)
    monkeypatch.setattr(c32, "onex_subcommand", lambda _s, **_kw: PRESENT)
    result = c32.run(
        lan_check=lambda _env, _scratch: Clause(c32.LAN_BIND_CLAUSE, True, "ok"),
        auth_check=auth_check,
    )
    blob = json.dumps(result.record()) + result.render()
    assert seen["value"].startswith(c32.PLANT_PREFIX)
    assert len(seen["value"]) > len(c32.PLANT_PREFIX) + 16
    assert seen["value"] not in blob
    assert result.verdict() == VERDICT_PASS


# ------------------------------------------------------------------ run()


def _run(monkeypatch, auth_check, *, lan_ok=True):
    monkeypatch.setattr(c32, "onex_cli", lambda: PRESENT)
    monkeypatch.setattr(c32, "onex_subcommand", lambda _s, **_kw: PRESENT)
    return c32.run(
        lan_check=lambda _env, _scratch: Clause(c32.LAN_BIND_CLAUSE, lan_ok, "lan"),
        auth_check=auth_check,
    )


def test_run_grades_the_cases_rather_than_waiting_on_a_token_precondition(
    monkeypatch,
) -> None:
    """The hard-coded OMN-19916 precondition is gone: the cases are graded today."""
    current = c32.credential_from_start(BASE, tokenless_server)

    def auth_check(env, scratch, value):
        obs = observe(tokenless_server, current=current, stale=current)
        return [PRESENT], [
            *c32.grade_auth_cases(obs, correlation_id=CID),
            c32.grade_planted_secret(findings()),
        ]

    result = _run(monkeypatch, auth_check)
    assert result.verdict() == VERDICT_FAIL
    assert not any(p.owner == "OMN-19916" for p in result.preconditions)
    assert [c.name for c in result.clauses][:5] == [name for name, _ in c32.AUTH_CASES]
    assert result.clauses[-1].name == c32.LAN_BIND_CLAUSE


def test_run_passes_only_when_every_case_the_secret_and_the_lan_bind_hold(
    monkeypatch,
) -> None:
    current, stale = ideal_credentials()

    def auth_check(env, scratch, value):
        obs = observe(ideal_server(), current=current, stale=stale)
        return [PRESENT], [
            *c32.grade_auth_cases(obs, correlation_id=CID),
            c32.grade_planted_secret(findings()),
        ]

    assert _run(monkeypatch, auth_check).verdict() == VERDICT_PASS
    assert _run(monkeypatch, auth_check, lan_ok=False).verdict() == VERDICT_FAIL


def test_run_with_an_absent_surface_is_precondition_absent_not_pass(
    monkeypatch,
) -> None:
    result = _run(monkeypatch, lambda _env, _scratch, _value: ([ABSENT], []))
    assert result.verdict() == VERDICT_PRECONDITION_ABSENT


def test_run_without_the_dashboard_command_starts_nothing(monkeypatch) -> None:
    monkeypatch.setattr(c32, "onex_cli", lambda: ABSENT)
    monkeypatch.setattr(c32, "onex_subcommand", lambda _s, **_kw: ABSENT)
    result = c32.run(
        lan_check=lambda *_a: pytest.fail("no LAN check without the command"),
        auth_check=lambda *_a: pytest.fail("no auth check without the command"),
    )
    assert result.verdict() == VERDICT_PRECONDITION_ABSENT


# ------------------------------------------------------- the exercise


def test_auth_check_starts_twice_on_one_port_and_replays_the_first_token(
    tmp_path, monkeypatch
) -> None:
    """Stale means the token of an earlier start, replayed against a later one on the same origin."""
    starts = []

    @contextmanager
    def start(env, argv, *, log_path=None, ready=None):
        starts.append((tuple(argv), log_path))
        token = STALE_TOKEN if len(starts) == 1 else CURRENT_TOKEN
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("served\n")
        yield base.DashboardStart(BASE, f"{BASE}/?token={token}", "served")

    servers = {1: ideal_server(STALE_TOKEN), 2: ideal_server()}

    def request(url, headers):
        return servers[len(starts)](url, headers)

    monkeypatch.setattr(c32, "identity_and_fixture", lambda _env, _store: [PRESENT])
    monkeypatch.setattr(c32, "plant_credential", lambda _env, _scratch, _value: PRESENT)
    monkeypatch.setattr(c32, "secret_findings", lambda **_kw: findings())
    monkeypatch.setattr(
        c32, "credential_table", lambda: (PRESENT, "local_inference_credentials")
    )
    home = tmp_path / "home"
    home.mkdir()
    pres, clauses = c32.exercise_auth_cases(
        {"HOME": str(home)},
        tmp_path,
        VALUE,
        start=start,
        request=request,
        lan_address=LAN,
    )
    assert all(p.present for p in pres)
    assert len(starts) == 2
    overlays = {argv[-1] for argv, _ in starts}
    assert len(overlays) == 1, "both starts must bind the same port"
    assert all(c.ok for c in clauses), [c for c in clauses if not c.ok]


# ---------------------------------------- the plant names a model (OMN-19916)


def test_the_plant_names_a_model_so_an_unattended_set_stores_the_key(
    tmp_path: Path,
) -> None:
    """Since OMN-20844 `onex secret set` refuses an OpenRouter key with no
    `--model` from a script (BYOK_MODEL_NOT_CHOSEN), so a plant without one
    never stores anything and C32 grades nothing."""
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run_input(argv, stdin, *, env, timeout):
        calls.append((list(argv), env))
        return 0, "Registered it as your openrouter route key", ""

    planted = c32.plant_credential(
        {"HOME": str(tmp_path)}, tmp_path, "v", run_input=run_input
    )
    assert planted.present
    [(argv, env)] = calls
    assert argv[:4] == ["onex", "secret", "set", c32.PLANT_REF]
    assert argv[argv.index("--model") + 1] == c32.PLANT_MODEL
    assert "v" not in argv
    assert all(
        env[k].startswith("http://127.0.0.1:") for k in ("HTTPS_PROXY", "https_proxy")
    )
