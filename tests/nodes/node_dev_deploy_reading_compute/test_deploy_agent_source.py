# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 11 reads what the deploy agent reports (OMN-23655): deployed, queued and failed, each with a failing control.

Exemplar for shape: test_receipt_and_probe.py and the golden chain test beside it. A merge counts as deployed when the
running build contains it and the agent's newest finished job succeeded; a merge the running build does not contain reads
UNKNOWN with its queue position while the agent has commands ahead, and NOT WORKING with the job id when the agent is
idle and its newest job failed. Each test names the single edit that turns it red.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_dev_deploy_reading import (
    ModelDeployAgentFacts,
    ModelDeploySource,
)
from omnimarket.models.model_reading import EnumReadingVerdict as V
from tests.nodes.node_dev_deploy_reading_compute.dev_deploy_support import (
    SHA_A,
    SHA_B,
    SHA_C,
    contract,
    lineage_fact,
    merge,
    probe,
    request,
    resolve_handler,
)

RUNNING = "e" * 40
JOB_LAST = "11111111-1111-4111-8111-111111111111"
JOB_LIVE = "22222222-2222-4222-8222-222222222222"
IDLE_GRACE = 7200


def facts(**edits: Any) -> dict[str, Any]:
    """An idle agent, whose newest finished job succeeded, running RUNNING built at 11:00."""
    base: dict[str, Any] = {
        "running_sha": RUNNING,
        "build_time": "2026-11-08T11:00:00Z",
        "in_flight_job_id": None,
        "in_flight_started_at": None,
        "commands_ahead": 0,
        "commands_ahead_unread_reason": None,
        "last_job_id": JOB_LAST,
        "last_job_status": "success",
        "last_job_completed_at": "2026-11-08T11:05:00Z",
    }
    return {**base, **edits}


def source(**edits: Any) -> dict[str, Any]:
    return {
        "rule": {"idle_grace_seconds": IDLE_GRACE},
        "facts": facts(**edits),
        "unread_reason": None,
    }


def read(
    *,
    merges: list[dict[str, Any]] | None = None,
    contained: bool = True,
    deploy: dict[str, Any] | None = None,
    lineage: list[dict[str, Any]] | None = None,
    **edits: Any,
):
    """One merge (SHA_A, 08:00, four hours old) read against the agent; ``contained`` is the clone's ancestry fact."""
    merges = merges if merges is not None else [merge(SHA_A, "2026-11-08T08:00:00Z")]
    if lineage is None:
        lineage = [
            lineage_fact(m["sha"], RUNNING, "ancestor" if contained else "not-ancestor")
            for m in merges
        ]
    handler = resolve_handler()()
    return handler.handle(
        request(
            merges=merges,
            receipts=[],
            probes=[probe(expected=RUNNING, observed=RUNNING)],
            lineage=lineage,
            deploy_source=deploy if deploy is not None else source(),
            **edits,
        )
    )


# --- deployed -------------------------------------------------------------------------------------------------


def test_deployed_merge_in_the_running_build_with_a_healthy_job_reads_working() -> None:
    reading = read()
    assert reading.verdict is V.WORKING
    assert reading.reasons == ()
    assert (reading.merges_checked, reading.merges_receipted) == (1, 1)


def test_deployed_control_dropping_the_containment_fact_does_not_read_working() -> None:
    reading = read(lineage=[])
    assert reading.verdict is V.UNKNOWN
    assert reading.reasons[0].startswith("deploy-lineage-unread:aaaaaaaaaaaa:")


def test_deployed_control_a_failed_newest_job_does_not_read_working() -> None:
    reading = read(deploy=source(last_job_status="failed"))
    assert reading.verdict is V.NOT_WORKING
    assert reading.reasons == (f"deploy-failed:aaaaaaaaaaaa:job-{JOB_LAST}",)


def test_deployed_the_job_still_in_flight_built_the_running_image_so_the_roll_is_unsettled() -> (
    None
):
    live = source(
        in_flight_job_id=JOB_LIVE,
        in_flight_started_at="2026-11-08T10:59:00Z",
        commands_ahead=1,
    )
    reading = read(deploy=live)
    assert reading.verdict is V.UNKNOWN
    assert reading.reasons == (f"deploy-settling:aaaaaaaaaaaa:job-{JOB_LIVE}",)
    older = source(
        in_flight_job_id=JOB_LIVE,
        in_flight_started_at="2026-11-08T11:30:00Z",
        commands_ahead=1,
    )
    assert read(deploy=older).verdict is V.WORKING


def test_deployed_the_probe_no_longer_compares_shas_but_still_fails_on_health() -> None:
    handler = resolve_handler()()
    good_probe = probe(expected=SHA_B, observed=SHA_C)
    base = request(
        merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
        receipts=[],
        probes=[good_probe],
        lineage=[lineage_fact(SHA_A, RUNNING)],
        deploy_source=source(),
    )
    assert handler.handle(base).verdict is V.WORKING
    broken = request(
        merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
        receipts=[],
        probes=[probe(expected=RUNNING, observed=RUNNING, ok=False)],
        lineage=[lineage_fact(SHA_A, RUNNING)],
        deploy_source=source(),
    )
    reading = handler.handle(broken)
    assert reading.verdict is V.NOT_WORKING
    assert reading.reasons == ("probe-failed:probe-1",)


# --- queued ---------------------------------------------------------------------------------------------------


def test_queued_merge_reads_unknown_with_its_queue_position_and_never_not_working() -> (
    None
):
    queued = source(
        in_flight_job_id=JOB_LIVE,
        in_flight_started_at="2026-11-08T11:30:00Z",
        commands_ahead=3,
    )
    # Even a merge ten days old: the agent's turn has not come, so the deadline of the old rule does not apply.
    ancient = [merge(SHA_A, "2026-10-29T08:00:00Z")]
    reading = read(merges=ancient, contained=False, deploy=queued)
    assert reading.verdict is V.UNKNOWN
    assert reading.reasons == ("deploy-queued:aaaaaaaaaaaa:ahead-3",)


def test_queued_control_with_the_queue_empty_the_same_old_merge_reads_not_working() -> (
    None
):
    ancient = [merge(SHA_A, "2026-10-29T08:00:00Z")]
    reading = read(merges=ancient, contained=False, deploy=source(commands_ahead=0))
    assert reading.verdict is V.NOT_WORKING
    assert reading.reasons[0].startswith("not-deployed:aaaaaaaaaaaa:idle-agent-age-")


def test_queued_a_job_in_flight_with_no_command_behind_it_counts_as_one_ahead() -> None:
    live = source(
        in_flight_job_id=JOB_LIVE,
        in_flight_started_at="2026-11-08T11:30:00Z",
        commands_ahead=0,
    )
    reading = read(contained=False, deploy=live)
    assert reading.reasons == ("deploy-queued:aaaaaaaaaaaa:ahead-1",)


def test_queued_an_unread_queue_is_unknown_never_zero() -> None:
    unread = source(
        commands_ahead=None, commands_ahead_unread_reason="lag sampler not ready"
    )
    reading = read(contained=False, deploy=unread)
    assert reading.verdict is V.UNKNOWN
    assert reading.reasons == (
        "deploy-queue-unread:aaaaaaaaaaaa:lag sampler not ready",
    )


def test_queued_an_idle_agent_inside_its_grace_reads_unknown_not_started() -> None:
    reading = read(
        merges=[merge(SHA_A, "2026-11-08T11:00:00Z")],
        contained=False,
    )
    assert reading.verdict is V.UNKNOWN
    assert reading.reasons == ("deploy-not-started:aaaaaaaaaaaa:age-3600s",)


# --- failed ---------------------------------------------------------------------------------------------------


def test_failed_roll_reads_not_working_with_the_job_id() -> None:
    reading = read(contained=False, deploy=source(last_job_status="failed"))
    assert reading.verdict is V.NOT_WORKING
    assert reading.reasons == (f"deploy-failed:aaaaaaaaaaaa:job-{JOB_LAST}",)
    assert reading.merges_receipted == 0


def test_failed_control_the_same_job_marked_success_does_not_name_a_failed_roll() -> (
    None
):
    reading = read(
        merges=[merge(SHA_A, "2026-11-08T11:00:00Z")],
        contained=False,
        deploy=source(last_job_status="success"),
    )
    assert not any(r.startswith("deploy-failed:") for r in reading.reasons)
    assert reading.verdict is V.UNKNOWN


def test_failed_job_does_not_fail_a_merge_the_agent_has_not_reached() -> None:
    queued_after_failure = source(
        last_job_status="failed",
        in_flight_job_id=JOB_LIVE,
        in_flight_started_at="2026-11-08T11:30:00Z",
        commands_ahead=2,
    )
    reading = read(contained=False, deploy=queued_after_failure)
    assert reading.verdict is V.UNKNOWN
    assert reading.reasons == ("deploy-queued:aaaaaaaaaaaa:ahead-2",)


# --- error chain ----------------------------------------------------------------------------------------------


def test_error_chain_unreadable_deploy_source_reads_unknown_with_its_reason() -> None:
    reading = read(
        deploy={
            "rule": {"idle_grace_seconds": IDLE_GRACE},
            "facts": None,
            "unread_reason": "deploy agent source unreadable: OSError",
        }
    )
    assert reading.verdict is V.UNKNOWN
    assert reading.reasons == (
        "deploy-source-unread:deploy agent source unreadable: OSError",
    )


def test_error_chain_a_queue_count_and_an_unread_reason_together_are_refused() -> None:
    with pytest.raises(ValidationError):
        ModelDeployAgentFacts.model_validate(
            facts(commands_ahead_unread_reason="and a count")
        )
    with pytest.raises(ValidationError):
        ModelDeployAgentFacts.model_validate(
            facts(commands_ahead=None, commands_ahead_unread_reason=None)
        )


def test_error_chain_a_source_with_neither_facts_nor_reason_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelDeploySource.model_validate(
            {"rule": {"idle_grace_seconds": 1}, "facts": None, "unread_reason": None}
        )
    with pytest.raises(ValidationError):
        ModelDeploySource.model_validate(
            {"rule": {"idle_grace_seconds": 0}, "facts": facts(), "unread_reason": None}
        )


def test_golden_chain_deploy_rule_leaves_the_declared_topics_alone() -> None:
    bus = contract()["event_bus"]
    assert bus["publish_topics"] == ["onex.evt.omnimarket.dev-deploy-read.v1"]
    reading = read()
    assert reading.read_at.isoformat().startswith("2026-11-08T12:00:00")
