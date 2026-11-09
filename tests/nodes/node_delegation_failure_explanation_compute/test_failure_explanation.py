# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18538: the failure-explanation compute, one test per acceptance criterion with its failing control."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.nodes.node_delegation_failure_explanation_compute as node_package
from omnimarket.models.delegation.model_delegation_client_verdict import (
    ModelDelegationClientVerdictRequest,
)
from omnimarket.nodes.node_delegation_failure_explanation_compute.handlers.handler_delegation_failure_explanation import (
    HandlerDelegationFailureExplanation,
    load_rules,
)
from omnimarket.nodes.node_delegation_failure_explanation_compute.models.model_delegation_failure_explanation import (
    COMMENTARY_MARKER,
    ModelDelegationFailureExplanation,
    ModelDelegationFailureExplanationRequest,
    ModelFailureAccountGroup,
    ModelFailureGroupFacts,
    ModelRecordedDelegationFailure,
)
from omnimarket.nodes.node_delegation_receipt_verdict_compute.models.model_receipt_verdict_request import (
    ModelReceiptVerdictRequest,
)
from omnimarket.projection.sqlite_database import _RUN_TRACE_DDL

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
NODES_ROOT = NODE_DIR.parent
SRC_ROOT = NODES_ROOT.parent
FIXTURE = Path(__file__).parent / "fixtures" / "sample_failures.json"
OUTPUT_MODULE = (
    "omnimarket.nodes.node_delegation_failure_explanation_compute"
    ".models.model_delegation_failure_explanation"
)
NODE_PACKAGE = "omnimarket.nodes.node_delegation_failure_explanation_compute"


def _request() -> ModelDelegationFailureExplanationRequest:
    return ModelDelegationFailureExplanationRequest.model_validate(
        json.loads(FIXTURE.read_text())["request"]
    )


def _explain() -> ModelDelegationFailureExplanation:
    return HandlerDelegationFailureExplanation().handle(_request())


# ---- AC1: only surfaces that exist today -------------------------------------------------------


def _surface_exists(surface: str) -> bool:
    """Whether `<table>.<column>` names a column of run_trace or a field of the client verdict."""
    table, _, column = surface.partition(".")
    if table == "run_trace":
        return (
            re.search(rf"^\s+{re.escape(column)}\s", _RUN_TRACE_DDL, re.M) is not None
        )
    if table == "delegation_client_verdict":
        return column in ModelDelegationClientVerdictRequest.model_fields
    return False


def test_ac1_every_input_field_comes_from_a_surface_that_exists_today() -> None:
    surfaces = load_rules().held_surfaces
    assert set(surfaces) == set(ModelRecordedDelegationFailure.model_fields)
    missing = [s for s in surfaces.values() if not _surface_exists(s)]
    assert missing == []


def test_ac1_failing_control_a_surface_that_does_not_exist_is_found_missing() -> None:
    assert not _surface_exists("run_trace.invented_column")
    assert not _surface_exists("new_failure_table.cause")
    assert _surface_exists("run_trace.failure_cause")


def test_ac1_the_node_declares_no_effect_and_no_io_dependency() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert metadata["capabilities"]["side_effect_class"] == "pure"
    assert metadata["capabilities"]["requires_network"] is False


# ---- AC2: the commentary marker on every output ------------------------------------------------


def test_ac2_every_output_and_every_group_carries_the_marker() -> None:
    result = _explain()
    assert result.commentary_marker == COMMENTARY_MARKER
    assert result.is_evidence is False
    assert result.groups
    assert all(
        g.commentary_marker == COMMENTARY_MARKER and g.is_evidence is False
        for g in result.groups
    )
    dumped = result.model_dump(mode="json")
    assert dumped["commentary_marker"] == COMMENTARY_MARKER
    assert all(g["commentary_marker"] == COMMENTARY_MARKER for g in dumped["groups"])


def test_ac2_the_contract_and_the_model_agree_on_the_marker() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["explanation_rules"]["commentary_marker"] == COMMENTARY_MARKER


def test_ac2_failing_control_an_output_cannot_be_built_without_or_with_another_marker() -> (
    None
):
    with pytest.raises(ValidationError):
        ModelFailureAccountGroup(
            commentary_marker="probe_result",  # type: ignore[arg-type]
            group_key="timeout",
            cause_recorded=True,
            count=1,
            correlation_ids=("r",),
            account="x",
        )
    with pytest.raises(ValidationError):
        ModelDelegationFailureExplanation(
            is_evidence=True,  # type: ignore[arg-type]
            correlation_id="c",
            total_failures=0,
            summary="s",
        )


# ---- AC3: no gate, closer or receipt path consumes it ------------------------------------------


def _importers_of_output(root: Path, own_dir: Path) -> list[Path]:
    """Python files under root, outside the node's own directory, that import its package."""
    found = []
    for path in root.rglob("*.py"):
        if own_dir in path.parents:
            continue
        if NODE_PACKAGE in path.read_text(encoding="utf-8", errors="replace"):
            found.append(path)
    return found


def _subscribers_of(topics: set[str], nodes_root: Path, own_dir: Path) -> list[Path]:
    """Node contracts, other than the node's own, that subscribe to any of the topics."""
    found = []
    for contract in nodes_root.glob("*/contract.yaml"):
        if contract.parent == own_dir:
            continue
        data = yaml.safe_load(contract.read_text(encoding="utf-8")) or {}
        subscribed = set((data.get("event_bus") or {}).get("subscribe_topics") or [])
        if subscribed & topics:
            found.append(contract)
    return found


def _own_topics() -> set[str]:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    return {contract["terminal_event"], *contract["event_bus"]["publish_topics"]}


def test_ac3_no_other_source_file_imports_the_node() -> None:
    assert _importers_of_output(SRC_ROOT, NODE_DIR) == []


def test_ac3_no_other_contract_subscribes_to_its_output_topic() -> None:
    assert _subscribers_of(_own_topics(), NODES_ROOT, NODE_DIR) == []
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["externally_consumed_topics"] == []


def test_ac3_failing_control_the_scans_do_catch_a_consumer(tmp_path: Path) -> None:
    gate = tmp_path / "node_some_gate" / "handlers" / "handler_gate.py"
    gate.parent.mkdir(parents=True)
    gate.write_text(f"from {OUTPUT_MODULE} import ModelDelegationFailureExplanation\n")
    assert _importers_of_output(tmp_path, NODE_DIR) == [gate]
    closer = tmp_path / "node_some_closer"
    closer.mkdir()
    (closer / "contract.yaml").write_text(
        yaml.safe_dump({"event_bus": {"subscribe_topics": sorted(_own_topics())[:1]}})
    )
    assert _subscribers_of(_own_topics(), tmp_path, NODE_DIR) == [
        closer / "contract.yaml"
    ]


def test_ac3_planted_explanation_in_an_evidence_position_is_refused() -> None:
    planted = _explain().model_dump(mode="json")
    # The receipt path takes a typed request; an explanation output in its place is refused.
    with pytest.raises(ValidationError):
        ModelReceiptVerdictRequest.model_validate(planted)
    # The client verdict (what a closer reads) refuses it too.
    with pytest.raises(ValidationError):
        ModelDelegationClientVerdictRequest.model_validate(planted)
    # And an explanation is not accepted as an explanation if the marker is stripped.
    stripped = {k: v for k, v in planted.items() if k != "commentary_marker"}
    assert (
        ModelDelegationFailureExplanation.model_validate(stripped).commentary_marker
        == COMMENTARY_MARKER
    )
    forged = {**planted, "is_evidence": True}
    with pytest.raises(ValidationError):
        ModelDelegationFailureExplanation.model_validate(forged)


# ---- AC4: grouping by recorded metadata, never by generated text -------------------------------


class _CategoryAssertingNarrator:
    """Injected break: a narrator whose text names a category different from every recorded cause."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def narrate(self, facts: ModelFailureGroupFacts) -> str:
        self.asked.append(facts.group_key)
        return "category=network_blip; this is really a user error"


def test_ac4_groups_are_keyed_by_the_recorded_cause() -> None:
    result = _explain()
    assert [g.group_key for g in result.groups] == [
        "provider_quota_exhausted",
        "timeout",
        "unknown",
    ]
    by_key = {g.group_key: g for g in result.groups}
    assert by_key["timeout"].correlation_ids == ("run-a1", "run-a2")
    # terminal_failure_cause wins; failure_cause of the run trace is the fallback.
    assert by_key["provider_quota_exhausted"].correlation_ids == ("run-b1",)
    assert result.total_failures == 4


def test_ac4_failing_control_generated_text_does_not_become_a_group_key() -> None:
    narrator = _CategoryAssertingNarrator()
    result = HandlerDelegationFailureExplanation(narrator=narrator).handle(_request())
    assert [g.group_key for g in result.groups] == [
        "provider_quota_exhausted",
        "timeout",
        "unknown",
    ]
    assert all("network_blip" not in g.group_key for g in result.groups)
    assert any("network_blip" in g.account for g in result.groups)
    assert sorted(narrator.asked) == ["provider_quota_exhausted", "timeout"]


def test_ac4_the_group_key_fields_are_recorded_fields_and_a_generated_one_is_refused() -> (
    None
):
    rules = load_rules()
    assert set(rules.group_key_fields) <= set(
        ModelRecordedDelegationFailure.model_fields
    )
    broken = {**rules.model_dump(), "group_key_fields": ["account"]}
    with pytest.raises(ValidationError):
        type(rules).model_validate(broken)


# ---- AC5: no recorded cause is an explicit unknown ---------------------------------------------


def test_ac5_a_failure_with_no_recorded_cause_is_unknown_and_says_so() -> None:
    unknown = next(g for g in _explain().groups if g.group_key == "unknown")
    assert unknown.cause_recorded is False
    assert unknown.correlation_ids == ("run-c1",)
    assert "no cause" in unknown.account
    # The record's free-text reason is not promoted to a cause.
    assert "free text" not in unknown.account


def test_ac5_failing_control_a_narrator_is_never_asked_to_explain_an_unknown_failure() -> (
    None
):
    narrator = _CategoryAssertingNarrator()
    result = HandlerDelegationFailureExplanation(narrator=narrator).handle(_request())
    unknown = next(g for g in result.groups if g.group_key == "unknown")
    assert "unknown" not in narrator.asked
    assert "network_blip" not in unknown.account
    assert unknown.cause_recorded is False


def test_ac5_every_cause_an_account_states_is_one_the_input_carries() -> None:
    request = _request()
    recorded = {
        c
        for f in request.failures
        for c in (f.terminal_failure_cause, f.failure_cause)
        if c
    }
    for group in _explain().groups:
        if group.cause_recorded:
            assert group.group_key in recorded
            assert f"cause {group.group_key}" in group.account
        else:
            assert not any(c in group.account for c in recorded)


def test_ac5_only_unknown_failures_read_unknown_only() -> None:
    request = ModelDelegationFailureExplanationRequest(
        correlation_id="c",
        failures=(ModelRecordedDelegationFailure(correlation_id="x"),),
    )
    result = HandlerDelegationFailureExplanation().handle(request)
    assert [g.group_key for g in result.groups] == ["unknown"]
    assert "1 with no recorded cause" in result.summary


def test_empty_request_is_an_empty_commentary() -> None:
    result = HandlerDelegationFailureExplanation().handle(
        ModelDelegationFailureExplanationRequest(correlation_id="c")
    )
    assert result.groups == ()
    assert result.total_failures == 0
    assert result.commentary_marker == COMMENTARY_MARKER
