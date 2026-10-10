"""The runtime decision payload upserts a readable ledger decision row."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
import yaml

from omnimarket.events.topics import CI_RED_TRIAGE_DECIDED_TOPIC_V1
from omnimarket.models.ci_red_triage import (
    EnumCiRedAction,
    EnumCiRedClass,
    ModelCiRedTriageDecided,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
    HandlerPrLifecycleStateReducer,
)
from omnimarket.projection.pr_ledger_projection import PR_LEDGER_PROJECTION_TABLE
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter


@pytest.mark.parametrize(
    ("applied", "final_state"), [(True, "fix_dispatched"), (False, "skipped")]
)
def test_decision_projects_and_redelivery_upserts(
    applied: bool, final_state: str
) -> None:
    decision_key = (
        "OmniNode-ai/omniclaude#2606@head:branch-claim-check / branch-claim-check"
    )
    event = ModelCiRedTriageDecided(
        correlation_id=uuid5(NAMESPACE_URL, "onex:ci-red-decision:" + decision_key),
        decision_key=decision_key,
        owner_key="cause:OmniNode-ai/omniclaude:123456789abc",
        event_id="a" * 64,
        repo="OmniNode-ai/omniclaude",
        pr_number=2606,
        head_sha="head",
        check="branch-claim-check / branch-claim-check",
        red_class=EnumCiRedClass.SHARED_CAUSE,
        action=EnumCiRedAction.START_CAUSE_OWNER
        if applied
        else EnumCiRedAction.JOINED_OWNER,
        action_applied=applied,
        orchestrator_run_id="ci-red-owner",
        members=(2606, 2607, 2608),
        initial_state="ci_red:shared_cause",
        evidence="class=shared_cause check=branch-claim-check action=start_cause_owner owner_key=cause members=(2606,2607,2608) run_id=ci-red-owner unread=base checks",
        observed_at="2026-10-08T10:00:00.123456Z",
    )
    database = InmemoryDatabaseAdapter()
    handler = HandlerPrLifecycleStateReducer()
    payload = {
        **event.model_dump(mode="json"),
        "_topic": CI_RED_TRIAGE_DECIDED_TOPIC_V1,
        "_db": database,
    }
    handler.handle_dict(payload)
    handler.handle_dict(payload)
    rows = database.query(
        PR_LEDGER_PROJECTION_TABLE, {"sweep_id": str(event.correlation_id)}
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["sweep_id"] == str(event.correlation_id)
    assert row["repo"] == event.repo
    assert row["pr_number"] == event.pr_number
    assert row["initial_state"] == "ci_red:shared_cause"
    assert row["action_taken"] == "fix"
    assert row["final_state"] == final_state
    assert row["evidence"] == event.evidence
    assert datetime.fromisoformat(str(row["found_at"])) == datetime(
        2026, 10, 8, 10, 0, 0, 123456, tzinfo=UTC
    )


def test_reducer_has_explicit_decision_topic_route() -> None:
    path = Path("src/omnimarket/nodes/node_pr_lifecycle_state_reducer/contract.yaml")
    contract = yaml.safe_load(path.read_text())
    assert CI_RED_TRIAGE_DECIDED_TOPIC_V1 in contract["event_bus"]["subscribe_topics"]
    entries = [
        entry
        for entry in contract["handler_routing"]["handlers"]
        if entry.get("topic") == CI_RED_TRIAGE_DECIDED_TOPIC_V1
    ]
    assert len(entries) == 1
    assert entries[0]["message_category"] == "event"


@pytest.mark.parametrize(
    ("action", "applied", "members", "claimed"),
    [
        (
            EnumCiRedAction.START_CAUSE_OWNER,
            True,
            (2606, 2607, 2608),
            {2606, 2607, 2608},
        ),
        (EnumCiRedAction.START_CAUSE_OWNER, False, (2606, 2607, 2608), set()),
        (EnumCiRedAction.JOINED_OWNER, False, (2606, 2607, 2608), set()),
    ],
)
def test_applied_start_projects_one_owner_claim_row_per_member(
    action: EnumCiRedAction, applied: bool, members: tuple[int, ...], claimed: set[int]
) -> None:
    decision_key = "OmniNode-ai/omniclaude#2608@head:branch-claim-check"
    owner_key = "cause:OmniNode-ai/omniclaude:123456789abc"
    event = ModelCiRedTriageDecided(
        correlation_id=uuid5(NAMESPACE_URL, "onex:ci-red-decision:" + decision_key),
        decision_key=decision_key,
        owner_key=owner_key,
        event_id="b" * 64,
        repo="OmniNode-ai/omniclaude",
        pr_number=2608,
        head_sha="head",
        check="branch-claim-check",
        red_class=EnumCiRedClass.SHARED_CAUSE,
        action=action,
        action_applied=applied,
        orchestrator_run_id="ci-red-owner",
        members=members,
        initial_state="ci_red:shared_cause",
        evidence="class=shared_cause",
        observed_at="2026-10-08T10:00:00Z",
    )
    database = InmemoryDatabaseAdapter()
    payload = {
        **event.model_dump(mode="json"),
        "_topic": CI_RED_TRIAGE_DECIDED_TOPIC_V1,
        "_db": database,
    }
    result = HandlerPrLifecycleStateReducer().handle_dict(payload)
    HandlerPrLifecycleStateReducer().handle_dict(payload)
    owner_sweep = str(uuid5(NAMESPACE_URL, "onex:ci-red-owner:" + owner_key))
    rows = database.query(PR_LEDGER_PROJECTION_TABLE, {"sweep_id": owner_sweep})
    assert result["rows_upserted"] == 1 + len(claimed)
    assert {row["pr_number"] for row in rows} == claimed
    assert len(rows) == len(claimed)
    assert all(
        f"claim=owner owner_key={owner_key}" in str(row["evidence"]) for row in rows
    )
    assert len(database.query(PR_LEDGER_PROJECTION_TABLE)) == 1 + len(claimed)
