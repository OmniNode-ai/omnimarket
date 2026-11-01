# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Historical grants, merge receipts and equivalent local and lab histories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from omnimarket.models.change_source.model_change_history import (
    EnumAccountType,
    ModelChangeHistory,
)
from omnimarket.models.release_gate.model_release_grant_facts import (
    EnumGateBoundary,
    EnumGrantState,
    EnumMergeMethod,
    ModelReleaseAuthorizationReceipt,
    ModelReleaseGrantFacts,
)
from omnimarket.nodes.node_release_grant_read_effect import (
    HandlerReadReleaseGrant,
    HandlerRecordAuthorizationReceipt,
)
from omnimarket.nodes.node_release_grant_read_effect.parsers import (
    events_from_ledger_rows,
    events_from_timeline,
)
from omnimarket.protocols.protocol_change_source import ProtocolChangeSource

from . import release_gate_fixtures as f

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src/omnimarket/nodes/node_release_grant_read_effect"


def _read(source: ProtocolChangeSource, **overrides: Any) -> ModelReleaseGrantFacts:
    return HandlerReadReleaseGrant(source, f.RecordingLedger(), f.FixedClock()).handle(
        f.read_request(**overrides)
    )


@pytest.mark.parametrize(
    ("login", "role"),
    [("fixture-operator", "operator"), ("fixture-cloud-owner", "cloud_owner")],
)
def test_role_holder_label_after_head_push_is_granted_without_disclosing_login(
    login: str,
    role: str,
) -> None:
    events = (f.standing_grant()[0], f.labeled("label-1", f.LABEL_AT, login))
    facts = _read(f.HistorySource(events))
    assert (facts.state, facts.reason) == (EnumGrantState.GRANTED, "granted")
    assert facts.applier_role == role
    assert facts.grant_event_id == "label-1"
    assert facts.read_at == f.NOW
    assert all(login not in facts.model_dump_json() for login in f.FIXTURE_LOGINS)


def test_label_before_a_new_head_push_does_not_grant_the_new_head() -> None:
    facts = _read(f.HistorySource(f.pushed_after_grant()), head_sha=f.NEW_HEAD)
    assert facts.state is EnumGrantState.NOT_GRANTED
    assert facts.reason == "label_before_head_push"


def test_old_head_is_blocked_after_a_new_head_was_pushed() -> None:
    facts = _read(f.HistorySource(f.pushed_after_grant()))
    assert facts.state is EnumGrantState.BLOCKED
    assert facts.reason == "head_is_not_current"


@pytest.mark.parametrize("label_first", [False, True])
def test_label_in_the_same_second_as_the_push_does_not_grant(label_first: bool) -> None:
    events = (
        f.standing_grant()[0],
        f.labeled("label-1", f.PUSH_AT, "fixture-operator"),
    )
    facts = _read(f.HistorySource(tuple(reversed(events)) if label_first else events))
    assert (facts.state, facts.reason) == (
        EnumGrantState.NOT_GRANTED,
        "label_before_head_push",
    )


@pytest.mark.parametrize(
    ("login", "account_type", "via_app", "reason"),
    [
        ("fixture-agent", EnumAccountType.USER, False, "applier_is_agent"),
        ("fixture-operator", EnumAccountType.BOT, False, "applier_not_a_person"),
        ("fixture-operator", EnumAccountType.APP, False, "applier_not_a_person"),
        ("fixture-operator", EnumAccountType.USER, True, "applier_not_a_person"),
        ("fixture-outsider", EnumAccountType.USER, False, "applier_not_in_role_list"),
    ],
)
def test_agent_app_bot_and_unlisted_appliers_cannot_grant(
    login: str,
    account_type: EnumAccountType,
    via_app: bool,
    reason: str,
) -> None:
    configured = f.policy(
        role_logins={
            "operator": ("fixture-operator", "fixture-agent"),
            "cloud_owner": ("fixture-cloud-owner",),
        }
    )
    events = (
        f.standing_grant()[0],
        f.labeled("label-1", f.LABEL_AT, login, account_type, via_app),
    )
    facts = _read(f.HistorySource(events), policy=configured)
    assert (facts.state, facts.reason) == (EnumGrantState.NOT_GRANTED, reason)


@pytest.mark.parametrize("text_kind", ["body", "comment"])
def test_grant_label_mentioned_in_text_is_not_a_label_event(text_kind: str) -> None:
    source = f.HistorySource(
        (f.standing_grant()[0],),
        bodies=(f"{text_kind}: fixture-release-grant",),
    )
    facts = _read(source)
    assert (facts.state, facts.reason) == (EnumGrantState.NOT_GRANTED, "no_grant_label")
    assert source.reads == 1


@pytest.mark.parametrize(
    ("reapplier", "state", "reason"),
    [
        (None, EnumGrantState.NOT_GRANTED, "label_removed"),
        ("fixture-agent", EnumGrantState.NOT_GRANTED, "applier_is_agent"),
        ("fixture-operator", EnumGrantState.GRANTED, "granted"),
    ],
)
def test_removal_and_reapplication_use_the_latest_label_event(
    reapplier: str | None,
    state: EnumGrantState,
    reason: str,
) -> None:
    events = (*f.standing_grant(), f.unlabeled("remove-1", f.LATER, "fixture-operator"))
    if reapplier is not None:
        events = (*events, f.labeled("label-2", f.NOW, reapplier))
    facts = _read(f.HistorySource(events))
    assert (facts.state, facts.reason) == (state, reason)
    if state is EnumGrantState.GRANTED:
        assert facts.grant_event_id == "label-2"


def test_unreadable_history_is_blocked() -> None:
    facts = _read(f.HistorySource(raise_on_read=True))
    assert (facts.state, facts.reason) == (
        EnumGrantState.BLOCKED,
        "label_history_unreadable",
    )


def test_present_label_without_push_history_is_blocked_and_fails_parity() -> None:
    historical = _read(f.HistorySource(f.standing_grant()))
    present = _read(f.PresentStateSource())
    assert historical.state is EnumGrantState.GRANTED
    assert (present.state, present.reason) == (
        EnumGrantState.BLOCKED,
        "head_push_not_in_history",
    )
    assert present.model_dump_json() != historical.model_dump_json()


def test_history_for_another_change_is_blocked() -> None:
    class MisdirectedSource(f.HistorySource):
        def change_history(
            self, repository: str, pull_request: int
        ) -> ModelChangeHistory:
            return (
                super()
                .change_history(repository, pull_request)
                .model_copy(update={"pull_request": 8})
            )

    facts = _read(MisdirectedSource(f.standing_grant()))
    assert (facts.state, facts.reason) == (
        EnumGrantState.BLOCKED,
        "history_for_another_change",
    )


@pytest.mark.parametrize("other_change", [(f.REPOSITORY, 8), ("fixture-other-repo", 7)])
def test_grant_on_another_change_cannot_admit_the_requested_repository_and_head(
    other_change: tuple[str, int],
) -> None:
    facts = _read(
        f.HistorySource(
            histories={
                (f.REPOSITORY, f.PULL_REQUEST): (f.standing_grant()[0],),
                other_change: f.standing_grant(),
            }
        )
    )
    assert facts.state is EnumGrantState.NOT_GRANTED
    assert facts.reason == "no_grant_label"
    assert (facts.repository, facts.pull_request, facts.head_sha) == (
        f.REPOSITORY,
        7,
        f.HEAD,
    )


def test_decision_ledger_row_carries_policy_and_boundary_without_logins() -> None:
    ledger = f.RecordingLedger()
    facts = HandlerReadReleaseGrant(
        f.HistorySource(f.standing_grant()), ledger, f.FixedClock()
    ).handle(f.read_request(boundary=EnumGateBoundary.RELEASE))
    ((kind, fields),) = ledger.rows
    assert kind == "release_gate_decision"
    assert {
        key: fields[key] for key in ("boundary", "state", "reason", "policy_digest")
    } == {
        "boundary": "release",
        "state": "granted",
        "reason": "granted",
        "policy_digest": f.policy().digest(),
    }
    assert fields["head_sha"] == facts.head_sha
    assert all(login not in json.dumps(fields) for login in f.FIXTURE_LOGINS)


@pytest.mark.parametrize("granted", [True, False])
def test_ledger_failure_blocks_a_grant_but_preserves_a_refusal(granted: bool) -> None:
    ledger = f.RecordingLedger(fail=True)
    events = f.standing_grant() if granted else (f.standing_grant()[0],)
    facts = HandlerReadReleaseGrant(
        f.HistorySource(events), ledger, f.FixedClock()
    ).handle(f.read_request())
    assert (facts.state, facts.reason) == (
        (EnumGrantState.BLOCKED, "decision_row_unwritable")
        if granted
        else (EnumGrantState.NOT_GRANTED, "no_grant_label")
    )
    assert ledger.attempts == 1
    assert ledger.rows == []


def test_unbound_ports_block_without_calling_any_port() -> None:
    with (
        patch.object(f.HistorySource, "change_history") as history,
        patch.object(f.RecordingLedger, "append_row") as append,
        patch.object(f.FixedClock, "now") as now,
    ):
        facts = HandlerReadReleaseGrant().handle(f.read_request())
    assert (facts.state, facts.reason) == (EnumGrantState.BLOCKED, "ports_not_bound")
    history.assert_not_called()
    append.assert_not_called()
    now.assert_not_called()


@pytest.mark.parametrize("method", list(EnumMergeMethod))
def test_merge_receipt_links_the_granted_head_to_the_resulting_commit(
    method: EnumMergeMethod,
) -> None:
    source, ledger, clock = (
        f.HistorySource(f.standing_grant()),
        f.RecordingLedger(),
        f.FixedClock(),
    )
    merge_sha = {
        EnumMergeMethod.SQUASH: "c",
        EnumMergeMethod.MERGE: "d",
        EnumMergeMethod.REBASE: "e",
    }[method] * 40
    result = HandlerRecordAuthorizationReceipt(source, ledger, clock).handle(
        f.receipt_request(merge_method=method, merge_commit_sha=merge_sha)
    )
    assert result.written is True
    assert result.facts.state is EnumGrantState.GRANTED
    assert result.facts.boundary is EnumGateBoundary.MERGE
    receipt = result.receipt
    assert receipt is not None
    assert (receipt.repository, receipt.pull_request, receipt.head_sha) == (
        f.REPOSITORY,
        7,
        f.HEAD,
    )
    assert receipt.merge_commit_sha == merge_sha != f.HEAD
    assert receipt.merge_method is method
    assert receipt.grant_event_id == "label-1"
    assert receipt.policy_digest == f.policy().digest()
    rows = [
        fields
        for kind, fields in ledger.rows
        if kind == "release_authorization_receipt"
    ]
    assert len(rows) == 1
    assert ModelReleaseAuthorizationReceipt.model_validate(rows[0]) == receipt
    assert source.reads == 1


def test_label_removed_before_merge_prevents_an_authorization_receipt() -> None:
    source = f.HistorySource(
        (*f.standing_grant(), f.unlabeled("remove-1", f.LATER, "fixture-operator"))
    )
    ledger = f.RecordingLedger()
    result = HandlerRecordAuthorizationReceipt(source, ledger, f.FixedClock()).handle(
        f.receipt_request()
    )
    assert result.written is False
    assert result.reason == "no_grant_at_merge:label_removed"
    assert result.receipt is None
    assert [kind for kind, _ in ledger.rows] == ["release_gate_decision"]


def test_receipt_append_failure_is_reported_after_a_successful_grant_read() -> None:
    source, clock = f.HistorySource(f.standing_grant()), f.FixedClock()
    decisions, receipts = f.RecordingLedger(), f.RecordingLedger(fail=True)
    reader = HandlerReadReleaseGrant(source, decisions, clock)
    result = HandlerRecordAuthorizationReceipt(
        source, receipts, clock, reader=reader
    ).handle(f.receipt_request())
    assert result.facts.state is EnumGrantState.GRANTED
    assert (result.written, result.reason, result.receipt) == (
        False,
        "receipt_unwritable",
        None,
    )
    assert receipts.attempts == 1
    assert receipts.rows == []


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"role_logins": {"operator": (), "cloud_owner": ()}},
            "applier_not_in_role_list",
        ),
        ({"grant_label": "fixture-other-grant"}, "no_grant_label"),
        (
            {"agent_identities": ("fixture-agent", "fixture-operator")},
            "applier_is_agent",
        ),
    ],
)
def test_policy_alone_changes_authorization_and_its_digest(
    overrides: dict[str, Any], reason: str
) -> None:
    source = f.HistorySource(f.standing_grant())
    baseline, changed = f.policy(), f.policy(**overrides)
    assert _read(source, policy=baseline).state is EnumGrantState.GRANTED
    facts = _read(source, policy=changed)
    assert (facts.state, facts.reason) == (EnumGrantState.NOT_GRANTED, reason)
    assert facts.policy_digest == changed.digest() != baseline.digest()


def test_node_and_shared_policy_sources_contain_no_fixture_logins() -> None:
    sources = [
        path
        for directory in (NODE_DIR, ROOT / "src/omnimarket/models/release_gate")
        for path in directory.rglob("*")
        if path.suffix in {".py", ".yaml"}
    ]
    assert sources
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert all(login not in text for login in f.FIXTURE_LOGINS), path


def test_contract_and_binding_table_declare_all_four_protocols() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    dependencies = contract["protocol_dependencies"]
    assert [item["name"] for item in dependencies] == ["BUS", "LED", "CHG", "CLK"]
    assert {item["name"]: item["protocol"] for item in dependencies} == {
        "BUS": "omnibase_core.protocols.event_bus.protocol_event_bus:ProtocolEventBus",
        "LED": "omnimarket.protocols.protocol_work_ledger_store:ProtocolWorkLedgerStore",
        "CHG": "omnimarket.protocols.protocol_change_source:ProtocolChangeSource",
        "CLK": "omnimarket.protocols.protocol_clock:ProtocolClock",
    }
    binding_file = (
        ROOT / "tests/nodes/node_binding_conformance_compute/data/binding_table.yaml"
    )
    table = yaml.safe_load(binding_file.read_text(encoding="utf-8"))
    assert [
        row["protocols"] for row in table["rows"] if row["node"] == NODE_DIR.name
    ] == [["BUS", "LED", "CHG", "CLK"]]
    assert NODE_DIR.name in table["checked_nodes"]


@pytest.mark.parametrize(
    "story", ["granted", "label_before_push", "removed", "agent_applied"]
)
def test_local_ledger_and_lab_timeline_produce_identical_grant_facts(
    story: str,
) -> None:
    events = f.standing_grant()
    head = f.HEAD
    expected = (EnumGrantState.GRANTED, "granted")
    if story == "label_before_push":
        head = f.NEW_HEAD
        events = (*events, f.pushed("push-2", f.LATER, head, "fixture-agent"))
        expected = (EnumGrantState.NOT_GRANTED, "label_before_head_push")
    elif story == "removed":
        events = (*events, f.unlabeled("remove-1", f.LATER, "fixture-operator"))
        expected = (EnumGrantState.NOT_GRANTED, "label_removed")
    elif story == "agent_applied":
        events = (events[0], f.labeled("label-1", f.LABEL_AT, "fixture-agent"))
        expected = (EnumGrantState.NOT_GRANTED, "applier_is_agent")
    local = events_from_ledger_rows(
        f.ledger_rows(events), repository=f.REPOSITORY, pull_request=7
    )
    lab = events_from_timeline(
        f.timeline_payload(events), repository=f.REPOSITORY, pull_request=7
    )
    local_facts = _read(f.HistorySource(local.events), head_sha=head)
    lab_facts = _read(f.HistorySource(lab.events), head_sha=head)
    assert (local_facts.state, local_facts.reason) == expected
    assert local_facts.model_dump_json() == lab_facts.model_dump_json()


def test_irrelevant_timeline_event_is_skipped_even_without_history_fields() -> None:
    history = events_from_timeline(
        [{"event": "commented"}], repository=f.REPOSITORY, pull_request=7
    )
    assert history.events == ()


@pytest.mark.parametrize("parser", ["timeline", "ledger"])
@pytest.mark.parametrize("problem", ["malformed", "naive_timestamp"])
def test_relevant_parser_items_require_valid_fields_and_aware_timestamps(
    parser: str, problem: str
) -> None:
    at = (
        "2026-10-27T10:05:00"
        if problem == "naive_timestamp"
        else f.LABEL_AT.isoformat()
    )
    if parser == "timeline":
        item = f.timeline_payload((f.standing_grant()[1],))[0]
        item["created_at"] = at
        if problem == "malformed":
            del item["actor"]
        with pytest.raises(ValueError, match=r"actor|timezone"):
            events_from_timeline([item], repository=f.REPOSITORY, pull_request=7)
    else:
        row = f.ledger_rows((f.standing_grant()[1],))[0]
        row["at"] = at
        if problem == "malformed":
            row["via_app"] = "invalid"
        with pytest.raises(ValueError, match=r"app flag|timezone"):
            events_from_ledger_rows([row], repository=f.REPOSITORY, pull_request=7)
