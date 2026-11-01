# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merged-and-running count: counts written by hand from the fixture below, one falsifier per acceptance line."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_merged_running_count_compute.handlers.handler_merged_running_count import (
    HandlerMergedRunningCount,
)
from omnimarket.nodes.node_merged_running_count_compute.models.model_merged_running_count import (
    ModelMergedRunningCountRequest,
    ModelMergedRunningCountResult,
    ModelMrcConfig,
    ModelMrcDay,
    ModelMrcEvidence,
    ModelMrcHost,
    ModelMrcLiveLane,
    ModelMrcPr,
)
from omnimarket.nodes.node_merged_running_count_effect.sources.source_contract_config import (
    load_contract_config,
)

# The clocks went back at 06:00Z on 2026-11-01, so a fixed -4h or -5h offset would put a merge on the wrong
# day. 2026-11-02T06:30Z is 01:30 EST on the 2nd.
NOW = datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


def _config(**changes: object) -> ModelMrcConfig:
    base = {
        "schema_id": "test.merged_running_count.v1",
        "operator_timezone": "America/New_York",
        "window_days": 2,
        "landing_branches": ["dev", "main"],
        "counted_pr_states": ["merged"],
        "sha_match_chars": 12,
        "no_evidence_source_reason": "no_evidence_source",
        "evidence_sources": [
            {
                "kind": "adopted",
                "repos": ["repo_a"],
                "missing_reason": "not_yet_adopted",
            },
            {
                "kind": "deployed",
                "repos": ["repo_b"],
                "missing_reason": "not_yet_deployed",
            },
        ],
    }
    base.update(changes)
    return ModelMrcConfig.model_validate(base)


def _merged(
    repo: str, number: int, at: datetime, sha: str = "", base: str = "dev"
) -> ModelMrcPr:
    return ModelMrcPr(
        repo=repo,
        number=number,
        state="merged",
        base_branch=base,
        merge_sha=sha,
        merged_at=at,
    )


A = _merged("repo_a", 10, datetime(2026, 11, 1, 17, 50, tzinfo=UTC), "aaaaaaaaaaaa1111")
B = _merged("repo_b", 20, datetime(2026, 11, 1, 18, 20, tzinfo=UTC), "bbbbbbbbbbbb2222")
C = _merged("repo_c", 30, datetime(2026, 11, 2, 1, 30, tzinfo=UTC), "cccccccccccc3333")
D = _merged("repo_c", 31, datetime(2026, 11, 2, 5, 30, tzinfo=UTC), "dddddddddddd4444")
EVIDENCE_A = ModelMrcEvidence(
    repo="repo_a",
    kind="adopted",
    source="STATUS lane=adopt-merged",
    number=10,
    merge_sha="aaaaaaaaaaaa",
    observed_at=datetime(2026, 11, 1, 17, 53, tzinfo=UTC),
)
HOSTS = (
    ModelMrcHost(host="h051", slots=6),
    ModelMrcHost(host="h053", slots=6),
    ModelMrcHost(host="h101", slots=None),
    ModelMrcHost(host="h105", slots=4, available=False),
)
LANES = (
    *(ModelMrcLiveLane(lane=f"full-{i}", host="h051") for i in range(6)),
    ModelMrcLiveLane(lane="two-a", host="h053"),
    ModelMrcLiveLane(lane="two-b", host="h053"),
    ModelMrcLiveLane(lane="stray", host="h999"),
)


def _request(**changes: object) -> ModelMergedRunningCountRequest:
    base: dict[str, object] = {
        "now": NOW,
        "config": _config(),
        "prs": (A, B, C, D),
        "evidence": (EVIDENCE_A,),
        "hosts": HOSTS,
        "live_lanes": LANES,
    }
    base.update(changes)
    return ModelMergedRunningCountRequest.model_validate(base)


def _run(**changes: object) -> ModelMergedRunningCountResult:
    return HandlerMergedRunningCount().handle(_request(**changes))


def _day(result: ModelMergedRunningCountResult, local_date: str) -> ModelMrcDay:
    return next(d for d in result.days if d.local_date == local_date)


def test_counts_by_operator_day_match_hand_count() -> None:
    result = _run()
    nov1, nov2 = _day(result, "2026-11-01"), _day(result, "2026-11-02")
    # Hand count, operator day 2026-11-01: A, B, C merged; A alone has evidence.
    assert (nov1.merged, nov1.running, nov1.not_running) == (3, 1, 2)
    assert [(r.repo, r.merged, r.running, r.not_running) for r in nov1.by_repo] == [
        ("repo_a", 1, 1, 0),
        ("repo_b", 1, 0, 1),
        ("repo_c", 1, 0, 1),
    ]
    # D merged at 05:30Z on the 2nd: 00:30 EST on the 2nd.
    assert (nov2.merged, nov2.running, nov2.not_running) == (1, 0, 1)
    # C is the 2nd by the UTC date and the 1st by the operator day; both are carried.
    c = next(m for m in nov1.merges if m.repo == "repo_c")
    assert (c.utc_date, c.local_date) == ("2026-11-02", "2026-11-01")
    assert nov1.utc_start == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    assert nov1.utc_end == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)


def test_reasons_and_evidence_kind_are_named_per_merge() -> None:
    merges = {
        m.repo: m for d in _run().days for m in d.merges if m.number in (10, 20, 30)
    }
    assert (merges["repo_a"].running, merges["repo_a"].evidence_kind) == (
        True,
        "adopted",
    )
    assert merges["repo_a"].evidence_source == "STATUS lane=adopt-merged"
    assert (merges["repo_b"].running, merges["repo_b"].reason) == (
        False,
        "not_yet_deployed",
    )
    # repo_c is in no evidence source: merged-not-shown-running, never running.
    assert (merges["repo_c"].running, merges["repo_c"].reason) == (
        False,
        "no_evidence_source",
    )


def test_handoff_armed_and_queued_states_never_count() -> None:
    """AC-1: a PR with a handed-off TERMINAL and an armed state but no merge changes neither count."""
    unmerged = tuple(
        ModelMrcPr(repo="repo_a", number=900, state=state, base_branch="dev")
        for state in ("handed_off", "armed", "queued", "open")
    )
    with_pr = _run(prs=(A, B, C, D, *unmerged))
    assert _day(with_pr, "2026-11-01").merged == 3
    assert _day(with_pr, "2026-11-01").running == 1
    only = _run(prs=unmerged, evidence=())
    assert sum(d.merged + d.running for d in only.days) == 0
    # The same PR with a merge and no running evidence: merged=1 running=0.
    merged = _run(
        prs=(_merged("repo_a", 900, datetime(2026, 11, 1, 20, 0, tzinfo=UTC)),),
        evidence=(),
    )
    day = _day(merged, "2026-11-01")
    assert (day.merged, day.running) == (1, 0)
    assert not [name for name in type(merged).model_fields if "hand" in name]
    assert not [name for name in type(day).model_fields if "hand" in name]


def test_merge_to_a_non_landing_branch_and_a_duplicate_are_not_counted() -> None:
    stacked = _merged(
        "repo_a", 77, datetime(2026, 11, 1, 20, 0, tzinfo=UTC), base="omn-feature"
    )
    result = _run(prs=(A, A, stacked))
    assert _day(result, "2026-11-01").merged == 1


def test_a_merge_after_the_supplied_clock_is_not_counted() -> None:
    later = _merged("repo_a", 78, NOW + timedelta(minutes=1))
    result = _run(prs=(A, later))
    assert sum(d.merged for d in result.days) == 1


def test_removing_the_evidence_row_moves_running_by_exactly_one() -> None:
    """AC-2."""
    with_row = _day(_run(), "2026-11-01")
    without = _day(_run(evidence=()), "2026-11-01")
    assert with_row.running - without.running == 1
    assert with_row.merged == without.merged
    assert [m.reason for m in without.merges if m.repo == "repo_a"] == [
        "not_yet_adopted"
    ]


def test_evidence_naming_only_the_merge_sha_matches_by_prefix() -> None:
    by_sha = EVIDENCE_A.model_copy(
        update={"number": None, "merge_sha": "aaaaaaaaaaaa9999"}
    )
    assert _day(_run(evidence=(by_sha,)), "2026-11-01").running == 1


def test_evidence_of_an_undeclared_kind_does_not_make_a_merge_run() -> None:
    other = EVIDENCE_A.model_copy(update={"kind": "hand_waved"})
    result = _run(evidence=(other,))
    assert _day(result, "2026-11-01").running == 0
    assert result.ignored_evidence == 1


def test_idle_slots_are_slots_minus_live_lane_slots_for_every_declared_host() -> None:
    """AC-3."""
    idle = {h.host: h for h in _run().idle_hosts}
    assert (idle["h051"].slots, idle["h051"].live_slots, idle["h051"].idle) == (6, 6, 0)
    assert (idle["h053"].slots, idle["h053"].live_slots, idle["h053"].idle) == (6, 2, 4)
    assert (idle["h101"].state, idle["h101"].idle) == ("unread", None)
    assert (idle["h105"].state, idle["h105"].idle) == ("out", 0)
    assert _run().idle_total == 4
    assert _run().lanes_on_unlisted_hosts == ("stray",)


def test_a_host_removed_from_the_declared_table_disappears() -> None:
    hosts = tuple(h for h in HOSTS if h.host != "h053")
    result = _run(hosts=hosts)
    assert "h053" not in {h.host for h in result.idle_hosts}
    assert result.idle_total == 0
    assert result.lanes_on_unlisted_hosts == ("stray", "two-a", "two-b")


def test_declared_timezone_and_window_change_the_output_without_code_change() -> None:
    """AC-4."""
    utc = _run(config=_config(operator_timezone="UTC", window_days=3))
    assert len(utc.days) == 3
    # In UTC, C (01:30Z on the 2nd) is on the 2nd with D (05:30Z).
    assert _day(utc, "2026-11-01").merged == 2
    assert _day(utc, "2026-11-02").merged == 2
    assert _day(_run(), "2026-11-01").merged == 3
    narrow = _run(config=_config(window_days=1))
    assert [d.local_date for d in narrow.days] == ["2026-11-02"]


def test_declared_evidence_sources_change_the_reason_and_the_kind_vocabulary() -> None:
    renamed = _config(
        no_evidence_source_reason="nothing_records_it",
        evidence_sources=[
            {"kind": "adopted", "repos": [], "missing_reason": "waiting"}
        ],
    )
    merges = _day(_run(config=renamed), "2026-11-01").merges
    assert {m.reason for m in merges if not m.running} == {"nothing_records_it"}
    assert next(m for m in merges if m.repo == "repo_a").running is True


def test_the_contract_declares_a_loadable_config() -> None:
    config = load_contract_config()
    assert config.operator_timezone == "America/New_York"
    assert config.counted_pr_states == ("merged",)
    result = HandlerMergedRunningCount().handle(
        ModelMergedRunningCountRequest(now=NOW, config=config, prs=(A,), hosts=HOSTS)
    )
    # The contract's repository lists are empty: nothing records it, so the merge is not running.
    day = _day(result, "2026-11-01")
    assert (day.merged, day.running) == (1, 0)


def test_models_refuse_extra_fields_and_naive_times() -> None:
    with pytest.raises(ValidationError):
        ModelMrcHost.model_validate({"host": "h1", "slots": 1, "handoffs": 3})
    with pytest.raises(ValidationError):
        ModelMrcPr(
            repo="r", number=1, state="merged", merged_at=datetime(2026, 11, 1, 1, 0)
        )
    with pytest.raises(ValidationError):
        _config(operator_timezone="Mars/Olympus")
