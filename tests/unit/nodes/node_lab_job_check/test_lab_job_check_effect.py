# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_job_check_effect: deadlines, per-lane evidence, the done-rule inputs, SELECTs only.

The ledger rows under ``tests/fixtures/lab_job_check`` are verbatim rows of the
rolling work ledger for 2026-10-04T18:00Z to 2026-10-05T13:00Z, cut after their
ninth cell. ``public.hook_events`` is not readable from a lane, so the hook
aggregates below are constructed, shaped like the store's own reading.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import yaml

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.enums.enum_lab_job import (
    EnumLabJobDoneCriterionKind,
    EnumLabJobKind,
    EnumLabJobLiveness,
    EnumLabJobState,
)
from omnimarket.enums.enum_lab_job_check import (
    EnumLabJobDeadline,
)
from omnimarket.models.lab_job import (
    ModelLabJobDoneCriterion,
)
from omnimarket.models.lab_job.model_lab_job_check import (
    ModelLabJobPrObservation,
)
from omnimarket.nodes.node_lab_job_check_effect.handlers import deadlines
from omnimarket.nodes.node_lab_job_check_effect.handlers import (
    postgres_lab_job_check_store as store_module,
)
from omnimarket.nodes.node_lab_job_check_effect.handlers.handler_lab_job_check_effect import (
    HandlerLabJobCheckEffect,
    check_config,
    load_lab_job_checked_topic,
)
from omnimarket.nodes.node_lab_job_check_effect.handlers.lane_evidence import (
    parse_ledger,
    terminal_after_claim,
)
from omnimarket.nodes.node_lab_job_check_effect.handlers.postgres_lab_job_check_store import (
    PostgresLabJobCheckStore,
    SelectOnlyConnection,
    StatementRefusedError,
    assert_select_only,
    parse_pr_target,
)
from omnimarket.nodes.node_lab_job_check_effect.models import (
    ModelLaneActivityReading,
    ModelRelayReading,
)
from tests.helpers.lab_job_check_fakes import (
    ADOPTED,
    FakeAdapter,
    FakeConnection,
    FakeStore,
    RecordingBus,
    _at,
    _job,
    _ledger_lines,
    _readings,
    _request,
    _spec,
    _state_row,
    _sweep,
)

pytestmark = pytest.mark.unit

_S = EnumLabJobState
FIXTURE = Path(__file__).parents[3] / "fixtures" / "lab_job_check"
NODE = Path(__file__).parents[4] / "src/omnimarket/nodes/node_lab_job_check_effect"


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------

D = EnumLabJobDeadline


@pytest.mark.parametrize(
    ("state", "in_state_s", "extra", "expected"),
    [
        (_S.QUEUED, 1799, {}, ()),
        (_S.QUEUED, 1800, {}, (D.DISPATCH,)),
        (_S.QUEUED, 60, {"next_dispatch": "2026-10-05T00:00:30Z"}, (D.BACKOFF,)),
        (_S.QUEUED, 60, {"next_dispatch": "2026-10-05T00:05:00Z"}, ()),
        (_S.DISPATCHED, 599, {}, ()),
        (_S.DISPATCHED, 600, {}, (D.CLAIM,)),
        (_S.STOPPING, 599, {}, ()),
        (_S.STOPPING, 600, {}, (D.STOP,)),
        (_S.ALERTING, 5000, {}, ()),  # no alert sent yet: nothing to resend
        (_S.ALERTING, 5000, {"alert_sent": "2026-10-05T00:00:00Z"}, (D.ALERT,)),
        (_S.ALERTING, 5000, {"alert_sent": "2026-10-05T01:20:00Z"}, ()),
        (_S.RETRYING, 5, {"next_dispatch": "2026-10-05T00:00:03Z"}, (D.BACKOFF,)),
        (_S.RETRYING, 5, {"next_dispatch": "2026-10-05T01:00:00Z"}, ()),
        (_S.RUNNING, 99999, {}, ()),
        (_S.STALLED, 99999, {}, ()),
        (_S.CHECKING, 99999, {}, ()),
    ],
)
def test_elapsed_deadlines_table(
    state: EnumLabJobState,
    in_state_s: int,
    extra: dict[str, str],
    expected: tuple[EnumLabJobDeadline, ...],
) -> None:
    entered = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
    job = _job("lj-" + "0" * 16, state, entered="2026-10-05T00:00:00Z", **extra)
    now = entered + timedelta(seconds=in_state_s)
    assert deadlines.elapsed_deadlines(job, now, check_config()) == expected


def test_the_contract_deadlines_are_the_plan_values() -> None:
    cfg = check_config()
    assert (
        cfg.dispatch_deadline_s,
        cfg.claim_deadline_s,
        cfg.stop_deadline_s,
        cfg.alert_deadline_s,
    ) == (1800, 600, 600, 900)
    assert cfg.relay_silence_threshold_s < cfg.default_stall_after_min * 60


# ---------------------------------------------------------------------------
# TERMINAL after CLAIM
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claim", "terminal", "counts"),
    [
        (
            "2026-10-05T00:47:39Z",
            "2026-10-04T21:52:25Z",
            False,
        ),  # older: previous dispatch
        (
            "2026-10-05T00:47:39Z",
            "2026-10-05T00:47:39Z",
            False,
        ),  # same instant: not newer
        ("2026-10-05T00:47:39Z", "2026-10-05T02:06:13Z", True),
        (None, "2026-10-05T02:06:13Z", True),  # no CLAIM to compare: still evidence
    ],
)
def test_a_terminal_counts_only_when_newer_than_the_claim(
    claim: str | None, terminal: str, counts: bool
) -> None:
    result = terminal_after_claim(_at(claim) if claim else None, _at(terminal))
    assert (result is not None) is counts
    assert terminal_after_claim(_at(claim) if claim else None, None) is None


def test_the_reused_lane_name_is_not_closed_by_the_previous_dispatchs_terminal() -> (
    None
):
    """dod-closeout-sweep wrote a TERMINAL at 21:52:25Z, then a new CLAIM at 00:47:39Z.

    Read independently, the newest TERMINAL before 00:50Z is the old one and the
    lane reads as terminated minutes after it began.
    """
    text = "\n".join(_ledger_lines())
    window_end = _at("2026-10-05T00:50:00Z")
    claims, terminals = parse_ledger(text, window_end)
    assert claims["dod-closeout-sweep"] == _at("2026-10-05T00:47:39Z")
    assert "dod-closeout-sweep" not in terminals
    # The same lane once the new dispatch has closed.
    claims, terminals = parse_ledger(text, _at("2026-10-05T02:10:00Z"))
    assert terminals["dod-closeout-sweep"] == _at("2026-10-05T02:06:13Z")


# ---------------------------------------------------------------------------
# A recorded window, swept
# ---------------------------------------------------------------------------

V = EnumLabJobLiveness


@pytest.mark.parametrize(
    ("name", "now", "activity", "relay_last", "verdict", "attributed"),
    [
        # Events at 04:58, sweep at 04:59: alive.
        (
            "fan",
            "2026-10-05T04:59:00Z",
            ("2026-10-05T04:58:00Z", 5, 7),
            "2026-10-05T04:58:30Z",
            V.ALIVE,
            True,
        ),
        # Same lane silent 32 minutes while the relay carried other traffic: dropped.
        (
            "fan",
            "2026-10-05T05:30:00Z",
            ("2026-10-05T04:58:00Z", 5, 7),
            "2026-10-05T05:29:30Z",
            V.DROPPED,
            True,
        ),
        # The relay itself went quiet: silence is not evidence.
        (
            "fan",
            "2026-10-05T05:30:00Z",
            ("2026-10-05T04:58:00Z", 5, 7),
            "2026-10-05T05:10:00Z",
            V.UNKNOWN_RELAY_SILENT,
            True,
        ),
        # No lane-attributed event since its CLAIM: not seen, so never alive and never dropped.
        (
            "validator",
            "2026-10-05T06:00:00Z",
            None,
            "2026-10-05T05:59:00Z",
            V.UNOBSERVABLE,
            False,
        ),
        # The new dispatch of a reused lane name is not closed by the old TERMINAL.
        (
            "dod",
            "2026-10-05T00:50:00Z",
            None,
            "2026-10-05T00:49:30Z",
            V.UNOBSERVABLE,
            False,
        ),
        # A TERMINAL newer than the CLAIM closes it.
        (
            "dod",
            "2026-10-05T02:10:00Z",
            None,
            "2026-10-05T02:09:30Z",
            V.TERMINATED,
            False,
        ),
        (
            "suite",
            "2026-10-05T07:04:00Z",
            None,
            "2026-10-05T07:03:30Z",
            V.TERMINATED,
            False,
        ),
    ],
)
async def test_recorded_window_verdicts(
    name: str,
    now: str,
    activity: tuple[str, int, int] | None,
    relay_last: str,
    verdict: EnumLabJobLiveness,
    attributed: bool,
) -> None:
    job = ADOPTED[name]
    readings = {}
    if activity is not None:
        lane = {"fan": "fan-9143-process-obs-top20"}[name]
        readings[lane] = ModelLaneActivityReading(
            lane=lane,
            last_event_at=_at(activity[0]),
            event_count=activity[1],
            attributed_count=activity[2],
        )
    store = FakeStore(
        [job],
        activity=readings,
        relay=ModelRelayReading(last_event_at=_at(relay_last), event_count=400),
    )
    record = (await _sweep(store, now))[job.job_id]
    assert record.verdict is verdict
    assert record.liveness is not None
    assert record.liveness.lane_attributed is attributed
    assert record.ledger is not None
    assert record.ledger.claimed_at == job.claimed_at


async def test_a_dropped_verdict_carries_every_input_the_reducer_and_a_reader_need() -> (
    None
):
    store = FakeStore(
        [ADOPTED["fan"]],
        activity={
            "fan-9143-process-obs-top20": ModelLaneActivityReading(
                lane="fan-9143-process-obs-top20",
                last_event_at=_at("2026-10-05T04:58:00Z"),
                event_count=5,
                attributed_count=7,
            )
        },
        relay=ModelRelayReading(
            last_event_at=_at("2026-10-05T05:29:30Z"), event_count=400
        ),
    )
    record = (await _sweep(store, "2026-10-05T05:30:00Z"))[ADOPTED["fan"].job_id]
    live = record.liveness
    assert live is not None
    assert live.last_event_age_s == 32 * 60
    assert live.silence_threshold_s == 20 * 60
    assert live.relay_state is not None
    assert live.relay_state.value == "carrying"
    assert live.hook_event_count == 5
    assert live.last_hook_event_at == _at("2026-10-05T04:58:00Z")
    assert record.ledger is not None
    assert record.ledger.terminal_row_id is None
    assert len(record.ledger.claim_row_id) == 64


async def test_the_closing_terminal_row_id_and_outcome_are_reported() -> None:
    store = FakeStore(
        [ADOPTED["suite"]],
        relay=ModelRelayReading(
            last_event_at=_at("2026-10-05T07:03:30Z"), event_count=9
        ),
    )
    record = (await _sweep(store, "2026-10-05T07:04:00Z"))[ADOPTED["suite"].job_id]
    assert record.ledger is not None
    assert record.ledger.terminal_outcome == "failed"
    assert record.ledger.terminal_at == _at("2026-10-05T07:03:56Z")
    assert record.ledger.terminal_row_id is not None


async def test_attribution_is_per_lane_not_per_window() -> None:
    """Other lanes carry attribution all through the window; this lane never did."""
    store = FakeStore(
        [ADOPTED["validator"]],
        activity={
            "some-other-lane": ModelLaneActivityReading(
                lane="some-other-lane",
                last_event_at=_at("2026-10-05T05:59:00Z"),
                event_count=300,
                attributed_count=300,
            )
        },
        relay=ModelRelayReading(
            last_event_at=_at("2026-10-05T05:59:00Z"), event_count=300
        ),
    )
    record = (await _sweep(store, "2026-10-05T06:00:00Z"))[ADOPTED["validator"].job_id]
    assert record.verdict is V.UNOBSERVABLE
    assert record.liveness is not None
    assert record.liveness.lane_attributed is False
    assert "attribution" in record.liveness.reason


async def test_a_job_with_no_claim_row_is_unobservable_never_alive() -> None:
    orphan = _job(
        "lj-" + "9" * 16, _S.RUNNING, claimed="2026-10-05T03:00:00Z", ticket="OMN-1"
    )
    record = (await _sweep(FakeStore([orphan]), "2026-10-05T06:00:00Z"))[orphan.job_id]
    assert record.verdict is V.UNOBSERVABLE
    assert record.ledger is None
    assert record.liveness is not None
    assert "no CLAIM row" in record.liveness.reason


# ---------------------------------------------------------------------------
# What is read for which state
# ---------------------------------------------------------------------------


async def test_each_state_gets_only_the_reads_the_plan_names() -> None:
    pr = ModelLabJobDoneCriterion(
        kind=EnumLabJobDoneCriterionKind.PR_MERGED, target="omnimarket#3434"
    )
    spec = _spec(pr)
    jobs = [
        _job("lj-" + "1" * 16, _S.QUEUED, kind=EnumLabJobKind.LANE, spec=spec),
        _job("lj-" + "2" * 16, _S.CHECKING, kind=EnumLabJobKind.LANE, spec=spec),
        _job("lj-" + "3" * 16, _S.RETRYING, kind=EnumLabJobKind.LANE, spec=spec),
        _job("lj-" + "4" * 16, _S.FAILED, kind=EnumLabJobKind.LANE, spec=spec),
        ADOPTED["fan"].model_copy(
            update={"job_id": "lj-" + "6" * 16, "state": _S.STALLED}
        ),
    ]
    observed = ModelLabJobPrObservation(
        target="omnimarket#3434",
        found=True,
        state="open",
        head_sha="e0f4ce4a5fa763b6907cda56afa01f162829209f",
        ci_verdict="PENDING",
        pending_contexts=("CI Summary",),
        read_at=_at("2026-10-05T05:59:00Z"),
    )
    records = await _sweep(
        FakeStore(jobs, prs={"omnimarket#3434": observed}), "2026-10-05T06:00:00Z"
    )
    queued, checking, retrying, failed, stalled = (records[j.job_id] for j in jobs)
    assert queued.liveness is None
    assert queued.pr_observations == ()
    assert failed.liveness is None
    assert failed.pr_observations == ()
    for record in (checking, retrying):
        assert record.liveness is None
        assert record.pr_observations == (observed,)
        assert record.pr_observations[0].head_sha is not None
        assert record.pr_observations[0].read_at == _at("2026-10-05T05:59:00Z")
    assert stalled.liveness is not None
    assert stalled.pr_observations == ()


async def test_a_pr_never_observed_is_reported_as_not_found_not_as_merged() -> None:
    spec = _spec(
        ModelLabJobDoneCriterion(
            kind=EnumLabJobDoneCriterionKind.CHECK_PASSING,
            target="omnimarket#1",
            check_context="CI Summary",
        )
    )
    job = _job("lj-" + "2" * 16, _S.CHECKING, kind=EnumLabJobKind.LANE, spec=spec)
    record = (await _sweep(FakeStore([job]), "2026-10-05T06:00:00Z"))[job.job_id]
    assert record.pr_observations == (
        ModelLabJobPrObservation(target="omnimarket#1", found=False),
    )


async def test_every_job_gets_a_record_with_its_elapsed_deadlines() -> None:
    queued = _job("lj-" + "1" * 16, _S.QUEUED, entered="2026-10-05T00:00:00Z")
    record = (await _sweep(FakeStore([queued]), "2026-10-05T01:00:00Z"))[queued.job_id]
    assert record.elapsed_deadlines == (D.DISPATCH,)
    assert record.job_state is _S.QUEUED
    assert record.attempt == 1


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


async def test_one_record_per_job_is_published_on_the_contract_topic_keyed_by_job() -> (
    None
):
    jobs = [
        _job("lj-" + "1" * 16, _S.QUEUED),
        _job("lj-" + "2" * 16, _S.CHECKING),
    ]
    bus = RecordingBus()
    sweep = await HandlerLabJobCheckEffect(FakeStore(jobs), bus=bus).handle(
        _request("2026-10-05T06:00:00Z")
    )
    assert sweep.published == 2
    topic = yaml.safe_load((NODE / "contract.yaml").read_text())["event_bus"][
        "publish_topics"
    ][0]
    assert {t for t, _, _ in bus.published} == {topic} == {load_lab_job_checked_topic()}
    assert [k for _, k, _ in bus.published] == [j.job_id.encode() for j in jobs]
    assert all(
        body["payload"]["check_id"] == "lab-job-check-test"
        for _, _, body in bus.published
    )


async def test_a_sweep_over_no_open_jobs_publishes_nothing() -> None:
    bus = RecordingBus()
    sweep = await HandlerLabJobCheckEffect(FakeStore([]), bus=bus).handle(
        _request("2026-10-05T06:00:00Z")
    )
    assert sweep.checked == ()
    assert bus.published == []


# ---------------------------------------------------------------------------
# SELECT statements only
# ---------------------------------------------------------------------------


async def test_a_full_sweep_through_the_real_store_issues_selects_only() -> None:
    claim = next(r for r in _readings() if r.row_lane == "fan-9143-process-obs-top20")
    spec = _spec(
        ModelLabJobDoneCriterion(
            kind=EnumLabJobDoneCriterionKind.PR_MERGED,
            target="OmniNode-ai/omnimarket#3434",
        )
    )
    running = ADOPTED["fan"]
    checking = _job("lj-" + "2" * 16, _S.CHECKING, kind=EnumLabJobKind.LANE, spec=spec)
    ledger = [
        {
            "row_id": claim.row_id,
            "row_ts": claim.row_ts,
            "row_lane": claim.row_lane,
            "raw_row": claim.raw_row,
        }
    ]
    log: list[str] = []
    conn = FakeConnection(
        log,
        {
            "set_config": [],
            "lab_job_state": [_state_row(running), _state_row(checking)],
            "'CLAIM'": ledger,
            "'TERMINAL'": [],
            "FILTER": [
                {
                    "lane": "fan-9143-process-obs-top20",
                    "last_event_at": _at("2026-10-05T04:58:00Z"),
                    "event_count": 5,
                    "attributed_count": 7,
                }
            ],
            "MAX(occurred_at) AS last_event_at": [
                {"last_event_at": _at("2026-10-05T05:29:30Z"), "event_count": 400}
            ],
            "omninode_internal.pr_state": [
                {
                    "repo": "omnimarket",
                    "pr_number": 3434,
                    "state": "open",
                    "head_sha": "e0f4ce4a5fa763b6907cda56afa01f162829209f",
                    "ci_verdict": "PENDING",
                    "red_contexts": "[]",
                    "pending_contexts": '["CI Summary"]',
                    "merged_at": "",
                    "observed_at": _at("2026-10-05T05:29:00Z"),
                }
            ],
        },
    )
    handler = HandlerLabJobCheckEffect(
        PostgresLabJobCheckStore(cast(AsyncpgAdapter, FakeAdapter(conn)))
    )
    sweep = await handler.handle(_request("2026-10-05T05:30:00Z"))

    statements = [entry for entry in log if not entry.startswith("BEGIN")]
    # Positive control: the sweep did read, through every surface.
    assert len(statements) >= 6
    touched = " ".join(statements)
    for surface in ("lab_job_state", "work_ledger_rows", "hook_events", "pr_state"):
        assert surface in touched
    assert all(s.upper().startswith("SELECT") for s in statements), statements
    assert all(
        entry == "BEGIN readonly=True" for entry in log if entry.startswith("BEGIN")
    )
    # The sweep's answer is the recorded one.
    by_id = {r.job_id: r for r in sweep.checked}
    assert by_id[running.job_id].verdict is V.DROPPED
    observation = by_id[checking.job_id].pr_observations[0]
    assert observation.found is True
    assert observation.pending_contexts == ("CI Summary",)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO omninode_internal.lab_job_state (job_id) VALUES ('x')",
        "UPDATE omninode_internal.lab_job_state SET state = 'done'",
        "DELETE FROM omninode_internal.lab_job_state",
        "WITH gone AS (DELETE FROM public.hook_events RETURNING 1) SELECT 1",
        "SELECT 1; DROP TABLE public.hook_events",
        "SELECT job_id FROM omninode_internal.lab_job_state FOR UPDATE",
        "SELECT pg_advisory_lock(1); SET ROLE postgres",
        "TRUNCATE public.hook_events",
        "  create table t (a int)",
    ],
)
async def test_the_select_only_guard_refuses_every_write(sql: str) -> None:
    log: list[str] = []
    conn = SelectOnlyConnection(FakeConnection(log, {"": []}))
    with pytest.raises(StatementRefusedError):
        await conn.fetch(sql)
    assert log == [], "a refused statement must never reach the driver"


def test_the_guard_accepts_every_statement_the_store_defines() -> None:
    sqls = [
        value
        for name, value in vars(store_module).items()
        if name.endswith("_SQL") and isinstance(value, str)
    ]
    assert len(sqls) == 7
    for sql in sqls:
        assert_select_only(sql)


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("omnimarket#3434", ("omnimarket", 3434)),
        ("OmniNode-ai/omnimarket#3434", ("omnimarket", 3434)),
    ],
)
def test_pr_targets_parse_with_or_without_owner(
    target: str, expected: tuple[str, int]
) -> None:
    assert parse_pr_target(target) == expected


@pytest.mark.parametrize("target", ["omnimarket", "omnimarket#", "#12", "a#b"])
def test_a_malformed_pr_target_is_refused(target: str) -> None:
    with pytest.raises(ValueError, match="repo#n"):
        parse_pr_target(target)


def test_the_hook_event_statements_key_on_the_lane_and_never_on_a_session_key() -> None:
    """The reader's AC4 holds for the per-lane statements too."""
    hook_sql = store_module._ACTIVITY_SQL + store_module._RELAY_SQL
    for forbidden in ("session_id", "correlation_id", "run_id", "entity_id"):
        assert forbidden not in hook_sql, f"{forbidden} is used as a query key"
    assert "payload->>'lane'" in hook_sql
    # Per lane, since that lane's own CLAIM.
    assert "c.claimed_at" in hook_sql


def test_the_script_still_exposes_the_moved_reader_half() -> None:
    import importlib.util

    path = Path(__file__).parents[4] / "scripts" / "lane_liveness_reader.py"
    spec = importlib.util.spec_from_file_location("lane_liveness_reader", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from omnimarket.nodes.node_lab_job_check_effect.handlers import lane_evidence

    for name in ("parse_ledger", "build_request", "gather", "activity_event_types"):
        assert getattr(module, name) is getattr(lane_evidence, name)
