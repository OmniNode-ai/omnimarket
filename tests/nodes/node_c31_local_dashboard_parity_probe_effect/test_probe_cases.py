# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C31 grading logic and the shared probe verdict rules, pinned offline (ported from the cloud repository's OMN-19989 tests, OMN-19966).

NO NETWORK and no process: a guard replaces the one runner the probes use for ``onex dashboard`` and fails any test
that reached it, and every test is offline and takes no skip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_c31_local_dashboard_parity_probe_effect.handlers import (
    probe_base as base,
)
from omnimarket.nodes.node_c31_local_dashboard_parity_probe_effect.handlers import (
    probe_c31 as c31,
)
from omnimarket.nodes.node_c31_local_dashboard_parity_probe_effect.handlers.probe_base import (
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_PRECONDITION_ABSENT,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_PRECONDITION_ABSENT,
    Clause,
    ExposureRead,
    Precondition,
    ProbeResult,
)

pytestmark = pytest.mark.unit

PRESENT = Precondition(name="p", present=True, detail="d", owner="OMN-1")
ABSENT = Precondition(name="q", present=False, detail="d", owner="OMN-2")


@pytest.fixture(autouse=True)
def no_dashboard_starts(monkeypatch: pytest.MonkeyPatch):
    """Fail any test that reached `onex dashboard` through the probes' runner (OMN-19984 AC6)."""
    started: list[list[str]] = []
    real = base._run

    def refuse(argv, *args, **kwargs):
        if list(argv[:2]) == ["onex", "dashboard"] and "--help" not in argv:
            started.append(list(argv))
            return 127, "", "unit tests never start a dashboard"
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(base, "_run", refuse)
    yield started
    assert not started, f"a unit test reached the dashboard runner: {started}"


def test_the_process_guard_sees_a_real_start_attempt(no_dashboard_starts) -> None:
    """Positive control for the guard: the dashboard starter does reach it."""
    with base.running_dashboard({}) as start:
        assert start.base_url is None
        assert "onex dashboard start --json" in start.detail
    assert no_dashboard_starts[0] == ["onex", "dashboard", "start", "--json"]
    no_dashboard_starts.clear()


def test_a_probe_that_graded_nothing_cannot_report_a_pass() -> None:
    """THE ONE THAT MATTERS. Preconditions hold, no clause graded -> raise."""
    r = ProbeResult(criterion="CX", preconditions=[PRESENT])
    with pytest.raises(AssertionError, match="no clause was graded"):
        r.verdict()


def test_an_absent_precondition_is_not_a_pass_and_not_a_clause_failure() -> None:
    r = ProbeResult(criterion="CX", preconditions=[PRESENT, ABSENT])
    assert r.verdict() == VERDICT_PRECONDITION_ABSENT
    assert r.exit_code() == EXIT_PRECONDITION_ABSENT
    assert r.exit_code() != EXIT_PASS


def test_a_graded_and_held_clause_set_passes() -> None:
    r = ProbeResult(
        criterion="CX",
        preconditions=[PRESENT],
        clauses=[Clause("a", True, "ok"), Clause("b", True, "ok")],
    )
    assert r.verdict() == VERDICT_PASS
    assert r.exit_code() == EXIT_PASS


def test_one_failed_clause_fails_the_whole_criterion() -> None:
    """Criteria are conjunctive. A majority of passing clauses is not a pass."""
    r = ProbeResult(
        criterion="CX",
        preconditions=[PRESENT],
        clauses=[Clause("a", True, "ok"), Clause("b", False, "no")],
    )
    assert r.verdict() == VERDICT_FAIL
    assert r.exit_code() == EXIT_FAIL


def test_an_absent_precondition_outranks_a_failing_clause() -> None:
    """If the surface is missing, a clause result is not trustworthy evidence."""
    r = ProbeResult(
        criterion="CX",
        preconditions=[ABSENT],
        clauses=[Clause("a", False, "no")],
    )
    assert r.verdict() == VERDICT_PRECONDITION_ABSENT


def test_the_three_exit_codes_are_distinct() -> None:
    assert len({EXIT_PASS, EXIT_FAIL, EXIT_PRECONDITION_ABSENT}) == 3


def test_the_record_round_trips_as_json_and_names_each_owner() -> None:
    r = ProbeResult(criterion="CX", preconditions=[PRESENT, ABSENT])
    payload = json.loads(json.dumps(r.record()))
    assert payload["verdict"] == VERDICT_PRECONDITION_ABSENT
    assert {p["owner"] for p in payload["preconditions"]} == {"OMN-1", "OMN-2"}


def test_the_render_leads_with_a_machine_readable_verdict() -> None:
    r = ProbeResult(criterion="C31", preconditions=[ABSENT])
    assert r.render().splitlines()[0] == f"C31_VERDICT={VERDICT_PRECONDITION_ABSENT}"


BASELINE = {"model": "claude-opus-4-6", "pricing_manifest_version": "2026-09-01"}


def _payload(**over: object) -> dict:
    base: dict = {
        "window": {"label": "7d", "start": None, "end": "2026-09-28T00:00:00Z"},
        "runs_total": 10,
        "runs_measured": 8,
        "runs_unknown_tokens": 1,
        "runs_unknown_spend": 1,
        "tokens_in": 1000,
        "tokens_out": 200,
        "spend_usd": "1.00",
        "counterfactual_usd": "5.00",
        "savings_usd": "4.00",
        "baseline_state": "resolved",
        "baseline": dict(BASELINE),
    }
    base.update(over)
    return base


def _named(clauses: list[Clause], fragment: str) -> Clause:
    return next(c for c in clauses if fragment in c.name)


def test_a_well_formed_metering_payload_passes_every_clause() -> None:
    clauses = c31._grade_metering_contract(_payload())
    assert clauses
    assert all(c.ok for c in clauses), [c for c in clauses if not c.ok]


def test_a_renamed_field_is_caught_rather_than_silently_read_as_absent() -> None:
    p = _payload()
    del p["tokens_out"]
    c = _named(c31._grade_metering_contract(p), "fields the pages read")
    assert not c.ok
    assert "tokens_out" in c.detail


def test_zero_savings_on_zero_measured_runs_is_the_defect_it_looks_like() -> None:
    """`0` renders as "we saved nothing"; the truth is "nothing was measured"."""
    p = _payload(
        runs_measured=0, savings_usd="0.00", spend_usd=None, counterfactual_usd=None
    )
    c = _named(c31._grade_metering_contract(p), "null, never zero")
    assert not c.ok


def test_null_savings_on_zero_measured_runs_is_correct() -> None:
    p = _payload(
        runs_measured=0, savings_usd=None, spend_usd=None, counterfactual_usd=None
    )
    c = _named(c31._grade_metering_contract(p), "null, never zero")
    assert c.ok


def test_a_savings_figure_without_a_baseline_is_refused() -> None:
    """The demo's hero label claimed "Verified savings" with no baseline named."""
    c = _named(
        c31._grade_metering_contract(_payload(baseline=None)), "names its baseline"
    )
    assert not c.ok


def test_a_savings_figure_without_a_pricing_manifest_version_is_refused() -> None:
    c = _named(
        c31._grade_metering_contract(_payload(baseline={"model": "x"})),
        "names its baseline",
    )
    assert not c.ok


def test_measured_runs_exceeding_total_runs_is_refused() -> None:
    c = _named(
        c31._grade_metering_contract(_payload(runs_measured=11)), "subset of total"
    )
    assert not c.ok


def test_arithmetic_that_does_not_close_is_refused() -> None:
    """counterfactual - spend must equal savings, or two figures disagree."""
    c = _named(
        c31._grade_metering_contract(_payload(savings_usd="3.00")), "equals savings"
    )
    assert not c.ok


def test_arithmetic_within_a_cent_is_accepted() -> None:
    c = _named(
        c31._grade_metering_contract(_payload(savings_usd="4.004")), "equals savings"
    )
    assert c.ok


def test_unparseable_figures_fail_rather_than_raise() -> None:
    c = _named(
        c31._grade_metering_contract(_payload(savings_usd="lots")), "equals savings"
    )
    assert not c.ok


CID = "c31c31c3-1000-4000-8000-000000000001"


def _runs(*rows: dict, status: int = 200, error: str | None = None) -> ExposureRead:
    return ExposureRead(c31.DECISIONS_TOPIC, status, tuple(rows), error)


def _overview(
    value: object = "3.00",
    status: int = 200,
    error: str | None = None,
    baseline: str = BASELINE["model"],
    rows: tuple[dict, ...] | None = None,
) -> ExposureRead:
    """The metering-summary exposure: a day row first, then the all row Overview binds.

    The day row carries a figure that can never match, so a grader that reads
    `rows[0]` instead of the `all` row fails every test that expects a pass.
    """
    if rows is None:
        rows = (
            {"window_kind": "day", "savings_usd": "99.99", "baseline_model": baseline},
            {
                "window_kind": "all",
                c31.METERING_SAVINGS_FIELD: value,
                "baseline_model": baseline,
            },
        )
    return ExposureRead(
        c31.METERING_SUMMARY_TOPIC, status, rows if status == 200 else (), error
    )


def _run_row(**over: object) -> dict:
    row = {
        "correlation_id": CID,
        "model_name": "probe-fixture",
        "tokens_input": 1000000,
        "tokens_output": 200000,
    }
    row.update(over)
    return row


def _metering(savings: object = "3.00") -> dict:
    return _payload(savings_usd=savings, spend_usd="1.00", counterfactual_usd="4.00")


def test_served_runs_row_and_matching_savings_grade_green() -> None:
    missing, clauses = c31.grade_dashboard(
        _runs(_run_row()), _overview(), correlation_id=CID, metering=_metering()
    )
    assert missing == []
    assert len(clauses) == 2
    assert all(c.ok for c in clauses), clauses


def test_planted_negative_a_missing_runs_row_fails() -> None:
    """The ticket's C31 negative: the Runs exposure served, without the receipt's row."""
    _missing, clauses = c31.grade_dashboard(
        _runs(), _overview(), correlation_id=CID, metering=_metering()
    )
    runs = _named(clauses, "Runs exposure")
    assert not runs.ok
    assert "none carries" in runs.detail


def test_a_row_for_another_correlation_id_is_not_the_receipts_row() -> None:
    other = _run_row(correlation_id="c31c31c3-1000-4000-8000-0000000000ff")
    _missing, clauses = c31.grade_dashboard(
        _runs(other), _overview(), correlation_id=CID, metering=_metering()
    )
    assert not _named(clauses, "Runs exposure").ok


@pytest.mark.parametrize("field", ["model_name", "tokens_input", "tokens_output"])
def test_a_runs_row_without_its_figures_fails(field: str) -> None:
    _missing, clauses = c31.grade_dashboard(
        _runs(_run_row(**{field: None})),
        _overview(),
        correlation_id=CID,
        metering=_metering(),
    )
    assert not _named(clauses, "Runs exposure").ok


def test_savings_two_cents_off_fails() -> None:
    _missing, clauses = c31.grade_dashboard(
        _runs(_run_row()), _overview("3.02"), correlation_id=CID, metering=_metering()
    )
    assert not _named(clauses, "Overview savings").ok


def test_savings_equal_once_rounded_to_cents_passes() -> None:
    _missing, clauses = c31.grade_dashboard(
        _runs(_run_row()), _overview("3.004"), correlation_id=CID, metering=_metering()
    )
    assert _named(clauses, "Overview savings").ok


@pytest.mark.parametrize(
    ("dashboard", "metering"), [(None, None), (None, "3.00"), ("3.00", None)]
)
def test_null_savings_on_either_side_is_never_parity(
    dashboard: object, metering: object
) -> None:
    _missing, clauses = c31.grade_dashboard(
        _runs(_run_row()),
        _overview(dashboard),
        correlation_id=CID,
        metering=_metering(metering),
    )
    assert not _named(clauses, "Overview savings").ok


def test_unserved_exposures_are_preconditions_naming_their_ticket_not_failed_clauses() -> (
    None
):
    """What omnimarket 0.4.291 answered on 2026-10-04: 503 for both."""
    missing, clauses = c31.grade_dashboard(
        _runs(status=503, error="projection_column_missing"),
        _overview(status=503, error="projection_table_missing"),
        correlation_id=CID,
        metering=_metering(),
    )
    assert clauses == []
    assert "omnimarket#3319" in missing[0].owner
    assert missing[1].owner.startswith("OMN-19976")


def test_a_tenant_refusal_names_the_identity_step() -> None:
    missing, _clauses = c31.grade_dashboard(
        _runs(status=422, error="tenant_not_configured"),
        _overview(),
        correlation_id=CID,
        metering=_metering(),
    )
    assert missing[0].owner.startswith("OMN-18699")


def test_a_200_without_a_rows_envelope_is_not_served() -> None:
    read = base.read_exposure(
        "http://127.0.0.1:1",
        c31.DECISIONS_TOPIC,
        token="t",
        get=lambda _u, _t, _h: (200, {"x": 1}),
    )
    assert not read.served
    assert read.absent() is not None


def _c31_run(
    monkeypatch,
    *,
    started=True,
    runs=None,
    savings=None,
    tenant="t-1",
    refresh_override=None,
):
    monkeypatch.setattr(c31, "onex_cli", lambda: PRESENT)
    monkeypatch.setattr(c31, "onex_subcommand", lambda _s, **_k: PRESENT)
    monkeypatch.setattr(c31, "seed_fixture_delegation", lambda _db, **_k: PRESENT)
    monkeypatch.setattr(
        c31, "onex_metering_json", lambda _db, **_k: (PRESENT, _metering())
    )
    seen = {"order": []}

    def identity(env):
        seen["home"] = env["HOME"]
        return (PRESENT, tenant) if tenant else (ABSENT, None)

    def dashboard(env, cid):
        seen["dashboard"] = (env["HOME"], cid)
        return (
            Precondition(
                "onex dashboard serves on loopback", started, "d", "OMN-19976"
            ),
            runs or _runs(_run_row()),
            savings or _overview(),
        )

    def refresh(db_path, tenant_id):
        seen["order"].append("refresh")
        seen["refresh"] = (db_path, tenant_id)
        return PRESENT

    monkeypatch.setattr(
        c31,
        "onex_metering_json",
        lambda _db, **_k: seen["order"].append("metering") or (PRESENT, _metering()),
    )
    return c31.run(
        dashboard=dashboard, identity=identity, refresh=refresh_override or refresh
    ), seen


def test_c31_run_reads_the_dashboard_on_the_seeded_scratch_home(monkeypatch) -> None:
    result, seen = _c31_run(monkeypatch)
    assert result.verdict() == VERDICT_PASS, result.render()
    assert seen["dashboard"] == (seen["home"], CID)


def test_c31_run_fails_on_the_planted_missing_row(monkeypatch) -> None:
    result, _seen = _c31_run(monkeypatch, runs=_runs())
    assert result.verdict() == VERDICT_FAIL


def test_c31_run_is_precondition_absent_while_the_store_lacks_declared_columns(
    monkeypatch,
) -> None:
    result, _seen = _c31_run(
        monkeypatch, runs=_runs(status=503, error="projection_column_missing")
    )
    assert result.verdict() == VERDICT_PRECONDITION_ABSENT
    assert any("omnimarket#3319" in p.owner for p in result.missing)


def test_c31_run_without_a_tenant_never_reads_the_dashboard(monkeypatch) -> None:
    result, seen = _c31_run(monkeypatch, tenant=None)
    assert result.verdict() == VERDICT_PRECONDITION_ABSENT
    assert "dashboard" not in seen


def test_c31_names_omn19976_as_the_owner_of_the_dashboard_command(monkeypatch) -> None:
    owners = []
    monkeypatch.setattr(
        c31, "onex_subcommand", lambda sub, owner: owners.append((sub, owner)) or ABSENT
    )
    monkeypatch.setattr(c31, "onex_cli", lambda: ABSENT)
    monkeypatch.setattr(c31, "onex_metering_json", lambda _db, **_k: (ABSENT, None))
    c31.run(
        dashboard=lambda *_a: pytest.fail("no dashboard read"),
        identity=lambda _env: pytest.fail("no init"),
        refresh=lambda *_a: pytest.fail("no refresh"),
    )
    assert ("dashboard", "OMN-19976") in owners


def test_overview_savings_binds_the_metering_summary_not_the_cost_overview() -> None:
    """OMN-19980 Step B (omnidash#364): Overview's Savings reads metering-summary.v1."""
    assert c31.METERING_SUMMARY_TOPIC == "onex.snapshot.projection.metering-summary.v1"
    assert not hasattr(c31, "OVERVIEW_SAVINGS_TOPIC")
    assert "metering-summary.v1" in c31.__doc__
    assert "omnidash#364" in c31.__doc__


def test_only_the_all_row_is_graded_never_a_day_row() -> None:
    day_only = _overview(
        rows=(
            {
                "window_kind": "day",
                "savings_usd": "3.00",
                "baseline_model": BASELINE["model"],
            },
        )
    )
    _missing, clauses = c31.grade_dashboard(
        _runs(_run_row()), day_only, correlation_id=CID, metering=_metering()
    )
    savings = _named(clauses, "Overview savings")
    assert not savings.ok
    assert "all" in savings.detail


def test_the_all_row_is_found_wherever_it_sits() -> None:
    rows = (
        {
            "window_kind": "all",
            "savings_usd": "3.00",
            "baseline_model": BASELINE["model"],
        },
        {
            "window_kind": "day",
            "savings_usd": "99.99",
            "baseline_model": BASELINE["model"],
        },
    )
    _missing, clauses = c31.grade_dashboard(
        _runs(_run_row()),
        _overview(rows=rows),
        correlation_id=CID,
        metering=_metering(),
    )
    assert _named(clauses, "Overview savings").ok


def test_a_baseline_model_that_differs_from_metering_fails_even_when_savings_match() -> (
    None
):
    _missing, clauses = c31.grade_dashboard(
        _runs(_run_row()),
        _overview("3.00", baseline="some-other-model"),
        correlation_id=CID,
        metering=_metering(),
    )
    savings = _named(clauses, "Overview savings")
    assert not savings.ok
    assert "some-other-model" in savings.detail
    assert BASELINE["model"] in savings.detail


def test_a_missing_baseline_on_the_all_row_fails() -> None:
    rows = ({"window_kind": "all", "savings_usd": "3.00"},)
    _missing, clauses = c31.grade_dashboard(
        _runs(_run_row()),
        _overview(rows=rows),
        correlation_id=CID,
        metering=_metering(),
    )
    assert not _named(clauses, "Overview savings").ok


def test_c31_refreshes_the_metering_summary_after_seeding_and_before_onex_metering(
    monkeypatch,
) -> None:
    result, seen = _c31_run(monkeypatch)
    assert seen["order"] == ["refresh", "metering"], seen["order"]
    db_path, tenant = seen["refresh"]
    assert str(db_path).endswith(".omninode/delegation/delegation.sqlite")
    assert tenant == "t-1"
    assert result.verdict() == VERDICT_PASS, result.render()


def test_c31_without_a_seeded_store_never_refreshes(monkeypatch) -> None:
    _result, seen = _c31_run(monkeypatch, tenant=None)
    assert "refresh" not in seen


def test_a_failed_refresh_is_a_precondition_not_a_pass(monkeypatch) -> None:
    gone = Precondition("metering summary refreshed", False, "boom", "OMN-19984")
    result, _seen = _c31_run(monkeypatch, refresh_override=lambda _db, _tenant: gone)
    assert result.verdict() == VERDICT_PRECONDITION_ABSENT
    assert gone in result.missing


def test_the_real_refresh_names_a_precondition_when_omnimarket_cannot_be_imported(
    monkeypatch,
) -> None:
    import builtins

    real = builtins.__import__

    def deny(name, *a, **k):
        if name.startswith("omnimarket.projection.sqlite_metering_summary"):
            raise ImportError("planted")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", deny)
    pre = c31.refresh_metering_summary_store(Path("/nonexistent/x.sqlite"), "t-1")
    assert not pre.present
    assert "planted" in pre.detail


def test_read_exposure_keeps_the_servers_error_code_and_filters_by_correlation() -> (
    None
):
    urls: list[str] = []

    def get(url, _timeout, _headers):
        urls.append(url)
        return 503, {"error": "projection_column_missing"}

    read = base.read_exposure(
        "http://127.0.0.1:9",
        c31.DECISIONS_TOPIC,
        token="t",
        correlation_id=CID,
        get=get,
    )
    assert read.error == "projection_column_missing"
    assert read.status == 503
    assert urls[0].endswith(f"?correlation_id={CID}")


def test_c31_runs_offline_and_never_reports_a_pass_it_did_not_earn() -> None:
    """On a machine with no local stack the probe may report PRECONDITION-ABSENT, never PASS without clauses."""
    result = c31.run(
        dashboard=lambda *_a: (ABSENT, _runs(), _overview()),
        identity=lambda _env: (ABSENT, None),
        refresh=lambda *_a: pytest.fail("offline: nothing to refresh"),
    )
    verdict = result.verdict()
    assert verdict in {VERDICT_PASS, VERDICT_FAIL, VERDICT_PRECONDITION_ABSENT}
    if verdict == VERDICT_PASS:
        assert result.clauses, "PASS with no graded clause is the false green"
