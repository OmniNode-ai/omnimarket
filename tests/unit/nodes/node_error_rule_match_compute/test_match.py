# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The five captured miss classes must remain explicit decisions."""

import json
from pathlib import Path

import pytest

from omnimarket.models.error_normalizer import (
    ModelErrorClassNormalizeRequest,
    ModelErrorEvent,
    ModelErrorNormalizerFacts,
)
from omnimarket.models.error_rule_match import (
    ModelErrorRuleContext,
    ModelErrorRuleMatchRequest,
)
from omnimarket.nodes.node_error_class_normalize_compute import (
    NodeErrorClassNormalizeCompute,
)
from omnimarket.nodes.node_error_rule_match_compute import NodeErrorRuleMatchCompute

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = Path(__file__).parent.parent / "node_error_class_normalize_compute/fixtures"


def normalize(text: str):
    facts = ModelErrorNormalizerFacts.model_validate_json(
        (CORPUS / "normalizer_facts.json").read_text()
    )
    return NodeErrorClassNormalizeCompute().handle(
        ModelErrorClassNormalizeRequest(
            event=ModelErrorEvent(source="tool", producer="Bash", text=text),
            facts=facts,
        )
    )


def table():
    from omnimarket.models.error_rule_match import ModelErrorRuleKeyVersion
    from omnimarket.nodes.node_error_rule_match_compute.overlay import (
        load_error_rule_table,
    )

    return load_error_rule_table(
        explicit=json.loads((FIXTURES / "seed_snapshot.json").read_text()),
        key_versions=tuple(
            ModelErrorRuleKeyVersion.model_validate(item)
            for item in json.loads((FIXTURES / "key_versions.json").read_text())
        ),
    )


@pytest.mark.parametrize("miss_id", ["1a", "1b", "2", "3", "4", "5"])
def test_five_misses_return_no_match(miss_id):
    cases = json.loads((CORPUS / "acceptance.json").read_text())["misses"]
    case = next(case for case in cases if case["id"] == miss_id)
    if miss_id == "5":
        fresh = json.loads((FIXTURES / "miss_5.json").read_text())
        event = ModelErrorEvent.model_validate(fresh["event"])
        facts = ModelErrorNormalizerFacts.model_validate_json(
            (CORPUS / "normalizer_facts.json").read_text()
        )
        normalized = NodeErrorClassNormalizeCompute().handle(
            ModelErrorClassNormalizeRequest(event=event, facts=facts)
        )
    else:
        normalized = normalize(case["records"][0])
    result = NodeErrorRuleMatchCompute().handle(
        ModelErrorRuleMatchRequest(normalized=normalized, table=table())
    )
    assert result.decision.status == "NO-MATCH"
    assert result.decision.class_key == normalized.class_key
    assert result.decision.key_version == normalized.key_version
    assert "action" not in result.decision.model_dump()


def test_exact_match_returns_row_rules_action_and_mode():
    snapshot = table()
    normalized = normalize("known fixture cause")
    result = (
        NodeErrorRuleMatchCompute()
        .handle(
            ModelErrorRuleMatchRequest(
                normalized=normalized,
                table=snapshot,
                context=ModelErrorRuleContext(
                    host_role="lab", repository="omnimarket", ticket_state="open"
                ),
            )
        )
        .decision
    )
    row = snapshot.rows[0]
    assert result.status == "MATCH"
    assert (result.row_id, result.rules, result.action, result.mode) == (
        row.id,
        row.sources,
        row.action,
        row.mode,
    )


@pytest.mark.parametrize("dimension", ["host_role", "repository", "ticket_state"])
def test_conditions_require_every_restricted_dimension(dimension):
    normalized = normalize("known fixture cause")
    context = {"host_role": "lab", "repository": "omnimarket", "ticket_state": "open"}
    context[dimension] = "other"
    result = NodeErrorRuleMatchCompute().handle(
        ModelErrorRuleMatchRequest(
            normalized=normalized,
            table=table(),
            context=ModelErrorRuleContext(**context),
        )
    )
    assert result.decision.status == "NO-MATCH"


def test_key_version_mismatch_is_no_match():
    normalized = normalize("known fixture cause").model_copy(
        update={"key_version": "0" * 64}
    )
    result = NodeErrorRuleMatchCompute().handle(
        ModelErrorRuleMatchRequest(normalized=normalized, table=table())
    )
    assert result.decision.status == "NO-MATCH"


@pytest.mark.parametrize("trigger", ["repeat", "state"])
def test_trigger_kind_and_state_fact_are_required(trigger):
    snapshot = table()
    row = snapshot.rows[0].model_copy(
        update={
            "trigger": snapshot.rows[0].trigger.model_copy(
                update={
                    "kind": trigger,
                    "state_check": "blocked" if trigger == "state" else None,
                }
            ),
            "applies_if": snapshot.rows[0].applies_if.model_copy(
                update={"host_roles": (), "repositories": (), "ticket_states": ()}
            ),
        }
    )
    command = ModelErrorRuleMatchRequest(
        normalized=normalize("known fixture cause"),
        table=snapshot.model_copy(update={"rows": (row,)}),
    )
    node = NodeErrorRuleMatchCompute()
    assert node.handle(command).decision.status == "NO-MATCH"
    context = ModelErrorRuleContext(trigger_kind=trigger)
    selected = node.handle(command.model_copy(update={"context": context}))
    assert selected.decision.status == ("NO-MATCH" if trigger == "state" else "MATCH")
    if trigger == "state":
        context = context.model_copy(update={"state_checks": ("blocked",)})
        assert (
            node.handle(command.model_copy(update={"context": context})).decision.status
            == "MATCH"
        )


def test_canonical_alias_matches_but_unresolved_and_explicit_none_do_not():
    normalized = normalize("known fixture cause")
    snapshot = table()
    context = ModelErrorRuleContext(
        host_role="lab", repository="omnimarket", ticket_state="open"
    )
    node = NodeErrorRuleMatchCompute()
    for alias_state in ("unresolved", "none"):
        prose = normalized.model_copy(
            update={
                "provisional": True,
                "canonical_key": None,
                "alias_state": alias_state,
                "class_key": "prose.fixture.unresolved",
            }
        )
        result = node.handle(
            ModelErrorRuleMatchRequest(
                normalized=prose, table=snapshot, context=context
            )
        )
        assert result.decision.status == "NO-MATCH"
        assert result.decision.class_key == prose.class_key
    resolved = prose.model_copy(
        update={"canonical_key": normalized.class_key, "alias_state": "canonical"}
    )
    result = node.handle(
        ModelErrorRuleMatchRequest(normalized=resolved, table=snapshot, context=context)
    )
    assert result.decision.status == "MATCH"
    assert result.decision.class_key == normalized.class_key
