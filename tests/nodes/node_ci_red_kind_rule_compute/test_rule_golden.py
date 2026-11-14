# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-24322: the rule pre-classifier's golden chain."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_ci_red_kind_rule_compute.handlers.handler_ci_red_kind_rule import (
    HandlerCiRedKindRule,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_answer import (
    ModelCiRedKindRuleAnswer,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_overlay import (
    ModelCiRedKindRuleOverlay,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_request import (
    ModelCiRedKindRuleRequest,
)

pytestmark = pytest.mark.unit

OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_ci_red_kind_rule_compute/overlay.example.yaml"
)


def _overlay() -> ModelCiRedKindRuleOverlay:
    doc: dict = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    return ModelCiRedKindRuleOverlay.model_validate(doc)


def _handler() -> HandlerCiRedKindRule:
    overlay = _overlay()
    return HandlerCiRedKindRule(load_overlay=lambda: overlay)


def _overlay_for(rules: list[dict]) -> ModelCiRedKindRuleOverlay:
    return ModelCiRedKindRuleOverlay.model_validate(
        {"schema_version": "ci_red_kind_rules.v1", "rules": rules}
    )


def classify(
    name: str,
    handler: HandlerCiRedKindRule,
    model_rung: Callable[[str], None],
) -> ModelCiRedKindRuleAnswer:
    answer = handler.handle(ModelCiRedKindRuleRequest(check_name=name))
    if answer.model_call_required:
        model_rung(name)
    return answer


def classify_without_rule(
    name: str,
    handler: HandlerCiRedKindRule,
    model_rung: Callable[[str], None],
) -> ModelCiRedKindRuleAnswer:
    answer = handler.handle(ModelCiRedKindRuleRequest(check_name=name))
    model_rung(name)
    return answer


def test_the_delegation_health_check_is_matched_by_rule_zero_with_no_model_call() -> (
    None
):
    handler = _handler()
    answer = handler.handle(
        ModelCiRedKindRuleRequest(check_name="delegation-health-check")
    )
    assert answer.ok is True
    assert answer.matched is True
    assert answer.kind == "delegation-health-gate"
    assert answer.rule_index == 0
    assert answer.model_call_required is False
    assert answer.refusal is None


def test_a_delegation_health_name_with_spaces_and_title_case_matches_the_same_rule() -> (
    None
):
    handler = _handler()
    answer = handler.handle(
        ModelCiRedKindRuleRequest(check_name="Delegation Health Check")
    )
    assert answer.ok is True
    assert answer.matched is True
    assert answer.kind == "delegation-health-gate"
    assert answer.rule_index == 0
    assert answer.model_call_required is False


def test_the_pin_resolvability_check_is_matched_by_rule_one_with_no_model_call() -> (
    None
):
    handler = _handler()
    answer = handler.handle(
        ModelCiRedKindRuleRequest(check_name="pypi-pin-resolvability")
    )
    assert answer.ok is True
    assert answer.matched is True
    assert answer.kind == "pin-unresolvable"
    assert answer.rule_index == 1
    assert answer.model_call_required is False
    assert answer.refusal is None


def test_a_name_no_rule_finds_is_answered_as_needing_the_model() -> None:
    handler = _handler()
    answer = handler.handle(
        ModelCiRedKindRuleRequest(check_name="Hostile Review Thread Gate")
    )
    assert answer.ok is True
    assert answer.matched is False
    assert answer.kind is None
    assert answer.rule_index is None
    assert answer.model_call_required is True
    assert answer.refusal is None


def test_the_first_rule_in_declared_order_whose_pattern_is_found_wins() -> None:
    overlay = _overlay_for(
        [
            {
                "kind": "first-declared-kind",
                "check_name_pattern": "shared-name",
                "ignore_case": True,
            },
            {
                "kind": "second-declared-kind",
                "check_name_pattern": "shared-name",
                "ignore_case": True,
            },
        ]
    )
    handler = HandlerCiRedKindRule(load_overlay=lambda: overlay)
    answer = handler.handle(ModelCiRedKindRuleRequest(check_name="shared-name"))
    assert answer.ok is True
    assert answer.matched is True
    assert answer.kind == "first-declared-kind"
    assert answer.rule_index == 0


def test_an_ignore_case_false_rule_misses_a_name_that_differs_only_in_case() -> None:
    overlay = _overlay_for(
        [
            {
                "kind": "case-kind",
                "check_name_pattern": "ABC",
                "ignore_case": False,
            },
        ]
    )
    handler = HandlerCiRedKindRule(load_overlay=lambda: overlay)
    answer = handler.handle(ModelCiRedKindRuleRequest(check_name="abc"))
    assert answer.ok is True
    assert answer.matched is False
    assert answer.kind is None
    assert answer.model_call_required is True


def test_an_ignore_case_true_rule_matches_a_name_that_differs_only_in_case() -> None:
    overlay = _overlay_for(
        [
            {
                "kind": "case-kind",
                "check_name_pattern": "ABC",
                "ignore_case": True,
            },
        ]
    )
    handler = HandlerCiRedKindRule(load_overlay=lambda: overlay)
    answer = handler.handle(ModelCiRedKindRuleRequest(check_name="abc"))
    assert answer.ok is True
    assert answer.matched is True
    assert answer.kind == "case-kind"
    assert answer.model_call_required is False


def test_the_composition_sends_only_the_unmatched_name_to_the_model_rung() -> None:
    handler = _handler()
    calls: list[str] = []
    delegation = classify("delegation-health-check", handler, calls.append)
    pin = classify("pypi-pin-resolvability", handler, calls.append)
    hostile = classify("Hostile Review Thread Gate", handler, calls.append)
    assert delegation.matched is True
    assert pin.matched is True
    assert hostile.matched is False
    assert hostile.model_call_required is True
    assert calls == ["Hostile Review Thread Gate"]


def test_an_injected_break_that_sends_rule_decided_names_to_the_model_rung() -> None:
    handler = _handler()
    calls: list[str] = []
    answer = classify_without_rule("delegation-health-check", handler, calls.append)
    assert answer.matched is True
    assert answer.kind == "delegation-health-gate"
    assert len(calls) == 1
    assert calls == ["delegation-health-check"]


def _write_shipped_overlay(root: Path) -> None:
    overlay_dir = root / "node_ci_red_kind_rule_compute"
    overlay_dir.mkdir()
    shutil.copyfile(OVERLAY_EXAMPLE, overlay_dir / "overlay.yaml")


def test_the_default_loader_reads_the_overlay_under_the_roots_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_shipped_overlay(tmp_path)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.delenv("CI_RED_KIND_RULES_OVERLAY", raising=False)
    handler = HandlerCiRedKindRule()
    answer = handler.handle(
        ModelCiRedKindRuleRequest(check_name="delegation-health-check")
    )
    assert answer.ok is True
    assert answer.matched is True
    assert answer.kind == "delegation-health-gate"
    assert answer.rule_index == 0


def test_the_pinned_overlay_file_named_by_the_env_wins_over_the_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_shipped_overlay(tmp_path)
    pinned = tmp_path / "pinned-overlay.yaml"
    pinned.write_text(
        "schema_version: ci_red_kind_rules.v1\n"
        "rules:\n"
        "  - kind: pinned-kind\n"
        "    check_name_pattern: 'delegation[- ]health'\n"
        "    ignore_case: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.setenv("CI_RED_KIND_RULES_OVERLAY", str(pinned))
    handler = HandlerCiRedKindRule()
    answer = handler.handle(
        ModelCiRedKindRuleRequest(check_name="delegation-health-check")
    )
    assert answer.ok is True
    assert answer.matched is True
    assert answer.kind == "pinned-kind"


def test_the_contract_names_the_handler_and_declares_local_invocation_with_no_bus() -> (
    None
):
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_ci_red_kind_rule_compute/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert contract["handler"]["class"] == "HandlerCiRedKindRule"
    operations = {
        handler["operation"]: handler["handler"]["name"]
        for handler in contract["handler_routing"]["handlers"]
    }
    assert operations == {"classify_ci_red_kind_by_rule": "HandlerCiRedKindRule"}
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["metadata"]["transport_type"] == "local"
