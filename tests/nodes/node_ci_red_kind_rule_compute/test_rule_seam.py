# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-24322: the rule node's kind seam."""

from __future__ import annotations

import pathlib

import pytest
import yaml

from omnimarket.nodes.node_ci_red_kind_rule_compute.handlers.handler_ci_red_kind_rule import (
    HandlerCiRedKindRule,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_overlay import (
    ModelCiRedKindRuleOverlay,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_request import (
    ModelCiRedKindRuleRequest,
)

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TAXONOMY_PATH = REPO_ROOT / "tests/fixtures/ci_red_kind_rule/kind_taxonomy.yaml"
OVERLAY_EXAMPLE = (
    REPO_ROOT
    / "src/omnimarket/nodes/node_ci_red_kind_rule_compute/overlay.example.yaml"
)


def _taxonomy() -> dict:
    return yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))


def _overlay() -> ModelCiRedKindRuleOverlay:
    doc: dict = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    return ModelCiRedKindRuleOverlay.model_validate(doc)


def test_every_kind_named_by_the_shipped_overlay_is_in_the_frozen_taxonomy() -> None:
    kinds = {rule.kind for rule in _overlay().rules}
    assert kinds <= set(_taxonomy()["taxonomy"])


def test_the_kinds_routing_does_not_declare_are_exactly_the_fixture_s_set() -> None:
    taxonomy = _taxonomy()
    kinds = {rule.kind for rule in _overlay().rules}
    assert kinds - set(taxonomy["routing_declared_kinds"]) == set(
        taxonomy["rule_kinds_not_in_routing"]
    )


def test_the_taxonomy_has_no_duplicate_names_and_holds_all_of_the_routing_kinds() -> (
    None
):
    taxonomy = _taxonomy()
    names = taxonomy["taxonomy"]
    assert len(names) == len(set(names))
    assert set(taxonomy["routing_declared_kinds"]) <= set(names)


def test_each_shipped_rule_example_names_a_kind_that_is_a_clean_nonempty_string() -> (
    None
):
    handler = HandlerCiRedKindRule(load_overlay=lambda: _overlay())
    for name in ("delegation-health-check", "pypi-pin-resolvability"):
        answer = handler.handle(ModelCiRedKindRuleRequest(check_name=name))
        assert answer.matched is True
        kind = answer.kind
        assert isinstance(kind, str)
        assert kind
        assert kind.strip() == kind


def test_every_kind_a_shipped_log_rule_names_is_declared_by_routing() -> None:
    log_rules = [rule for rule in _overlay().rules if rule.log_pattern is not None]
    assert log_rules
    assert {rule.kind for rule in log_rules} <= set(
        _taxonomy()["routing_declared_kinds"]
    )
