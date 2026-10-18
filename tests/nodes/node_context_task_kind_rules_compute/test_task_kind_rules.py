# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task-kind rule evaluation over a typed caller scope (OMN-21131, plan task TC.8)."""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from pydantic import ValidationError

from omnimarket.enums.enum_context_task_kind_operator import (
    EnumContextTaskKindOperator,
)
from omnimarket.models.context_pack.model_context_task_kind_condition import (
    ModelContextTaskKindCondition,
    path_glob_matches,
)
from omnimarket.models.context_pack.model_context_task_kind_rule import (
    ModelContextTaskKindRule,
)
from omnimarket.nodes.node_context_profile_resolver_effect.handlers.handler_context_profile_resolver import (
    HandlerContextProfileResolver,
)
from omnimarket.nodes.node_context_profile_resolver_effect.models.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.nodes.node_context_task_kind_rules_compute.handlers.handler_context_task_kind_rules import (
    HandlerContextTaskKindRules,
)
from omnimarket.nodes.node_context_task_kind_rules_compute.models import (
    EnumContextTaskKindRulesStatus,
    ModelContextTaskKindRulesRequest,
    ModelContextTaskKindRulesResult,
)
from omnimarket.pack.model_context_profile import ModelContextProfile

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]

F = EnumContextFactor


def _profile(name: str) -> ModelContextProfile:
    result = HandlerContextProfileResolver().handle(
        ModelContextProfileResolveRequest(profile_name=name)
    )
    assert result.profile is not None, result.errors
    return result.profile


def _evaluate(
    profile: ModelContextProfile, name: str = "fixture", **scope: Any
) -> ModelContextTaskKindRulesResult:
    return HandlerContextTaskKindRules().handle(
        ModelContextTaskKindRulesRequest(
            profile_name=name,
            profile=profile,
            caller_scope=ModelContextCallerScope(**scope),
        )
    )


def _rule(**overrides: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "rule_id": "r1",
        "task_kind": "node_compute",
        "conditions": [
            {"scope_field": "node_archetype", "operator": "equals", "value": "compute"}
        ],
        "factors": ["exemplar"],
    }
    rule.update(overrides)
    return rule


def _fixture_profile(*rules: dict[str, Any]) -> ModelContextProfile:
    return ModelContextProfile.model_validate(
        {
            "model_id": "fixture-consumer",
            "required_factors": ["lane_rules"],
            "optional_factors": ["invariants", "canonical_shapes", "exemplar"],
            "factor_precedence": [
                "lane_rules",
                "invariants",
                "canonical_shapes",
                "exemplar",
            ],
            "task_kind_rules": list(rules),
        }
    )


# --- path-glob goldens (port item AP-S8) -------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("services/**/*.py", "services/app.py", True),
        ("services/**/*.py", "services/search/app.py", True),
        ("services/**/*.py", "tests/test_foo.py", False),
        ("*.py", "app.py", True),
        ("*.py", "services/app.py", False),
        ("a/**/b", "a/x/y/b", True),
        ("a/**/b", "a/b", True),
        ("a/**/b", "a/x/y/c", False),
        ("**/tests/**", "tests/unit/test_x.py", True),
        ("**/tests/**", "omnimarket/tests/unit/test_x.py", True),
        ("**/tests/**", "omnimarket/testsuite/x.py", False),
        ("server/consumers/**", "server/consumers/kafka.ts", True),
        ("file?.py", "file1.py", True),
        ("file?.py", "file/.py", False),
        ("a.b", "aXb", False),
    ],
)
def test_path_glob_goldens(pattern: str, path: str, expected: bool) -> None:
    assert path_glob_matches(pattern, path) is expected


@pytest.mark.parametrize(
    "pattern",
    ["", "/abs/*.py", "dir/", "a//b", "a**b/c", "x/***", "src/[ab].py", "{a,b}.py"],
)
def test_a_malformed_glob_raises_at_validation(pattern: str) -> None:
    with pytest.raises(ValidationError):
        ModelContextTaskKindCondition.model_validate(
            {"scope_field": "request_paths", "operator": "glob", "value": pattern}
        )


# --- the rule vocabulary ------------------------------------------------------


@pytest.mark.parametrize(
    "condition",
    [
        {"scope_field": "prompt_keywords", "operator": "equals", "value": "deploy"},
        {"scope_field": "conversation_domain", "operator": "equals", "value": "ops"},
        {"scope_field": "task_class", "operator": "contains", "value": "code"},
        {"scope_field": "task_class", "operator": "regex", "value": ".*"},
        {"scope_field": "repository", "operator": "glob", "value": "omni*"},
    ],
)
def test_a_selector_outside_the_vocabulary_fails_validation(
    condition: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ModelContextTaskKindRule.model_validate(_rule(conditions=[condition]))


def test_an_unknown_condition_logic_fails_validation() -> None:
    with pytest.raises(ValidationError):
        ModelContextTaskKindRule.model_validate(_rule(condition_logic="most"))


def test_a_rule_may_not_claim_the_unknown_task_kind() -> None:
    with pytest.raises(ValidationError):
        ModelContextTaskKindRule.model_validate(_rule(task_kind="unknown"))


def test_a_rule_factor_must_be_optional_in_its_profile() -> None:
    with pytest.raises(ValidationError, match="not optional_factors"):
        _fixture_profile(_rule(factors=["lane_rules"]))
    with pytest.raises(ValidationError, match="not optional_factors"):
        _fixture_profile(_rule(factors=["adrs"]))


def test_rule_ids_are_unique_in_a_profile() -> None:
    with pytest.raises(ValidationError, match="repeat rule ids"):
        _fixture_profile(_rule(), _rule())


# --- evaluation ---------------------------------------------------------------


def test_an_unmatched_request_is_unknown_and_loads_only_unconditional_factors() -> None:
    profile = _profile("agent_spawn")
    result = _evaluate(profile, "agent_spawn", repository="omnimarket")
    assert result.status is EnumContextTaskKindRulesStatus.OK
    assert result.task_kind == "unknown"
    assert result.matched_rules == ()
    assert result.admitted_factors == (F.LANE_RULES, F.INVARIANTS)
    assert set(result.withheld_factors) == {F.CANONICAL_SHAPES, F.EXEMPLAR}


def test_a_matched_rule_names_the_task_kind_and_admits_its_factors() -> None:
    result = _evaluate(_profile("agent_spawn"), "agent_spawn", node_archetype="effect")
    assert result.task_kind == "node_effect"
    assert [r.rule_id for r in result.matched_rules] == ["archetype_effect"]
    assert result.admitted_factors == (
        F.LANE_RULES,
        F.INVARIANTS,
        F.CANONICAL_SHAPES,
        F.EXEMPLAR,
    )
    assert result.withheld_factors == ()


def test_equals_is_exact_not_a_substring() -> None:
    profile = _fixture_profile(_rule())
    for near in ("compute_node", "comp", "Compute", " compute"):
        result = _evaluate(profile, node_archetype=near)
        assert result.task_kind == "unknown", near
    assert _evaluate(profile, node_archetype="compute").task_kind == "node_compute"


def test_an_absent_descriptor_never_matches_even_when_negated() -> None:
    negated = {
        "scope_field": "lane_kind",
        "operator": "equals",
        "value": "landing",
        "negate": True,
    }
    profile = _fixture_profile(_rule(conditions=[negated]))
    absent = _evaluate(profile)
    assert absent.task_kind == "unknown"
    condition = absent.unmatched_rules[0].conditions[0]
    assert condition.absent is True
    assert condition.matched is False
    assert _evaluate(profile, lane_kind="build").task_kind == "node_compute"
    assert _evaluate(profile, lane_kind="landing").task_kind == "unknown"


def test_condition_logic_all_and_any() -> None:
    conditions = [
        {"scope_field": "repository", "operator": "equals", "value": "omnimarket"},
        {"scope_field": "request_paths", "operator": "glob", "value": "src/**/*.py"},
    ]
    all_profile = _fixture_profile(_rule(conditions=conditions))
    any_profile = _fixture_profile(_rule(conditions=conditions, condition_logic="any"))
    only_repo = {"repository": "omnimarket", "request_paths": ("README.md",)}
    both = {"repository": "omnimarket", "request_paths": ("README.md", "src/a/b.py")}
    assert _evaluate(all_profile, **only_repo).task_kind == "unknown"
    assert _evaluate(all_profile, **both).task_kind == "node_compute"
    assert _evaluate(any_profile, **only_repo).task_kind == "node_compute"


def test_priority_then_rule_id_names_the_task_kind() -> None:
    profile = _fixture_profile(
        _rule(rule_id="b_low", task_kind="kind_b", priority="low"),
        _rule(rule_id="z_high", task_kind="kind_z", priority="high"),
        _rule(rule_id="a_high", task_kind="kind_a", priority="high"),
    )
    result = _evaluate(profile, node_archetype="compute")
    assert [r.rule_id for r in result.matched_rules] == ["a_high", "z_high", "b_low"]
    assert result.task_kind == "kind_a"


def test_the_result_does_not_depend_on_rule_declaration_order() -> None:
    rules = [
        _rule(rule_id="r1", task_kind="k1", priority="medium"),
        _rule(rule_id="r2", task_kind="k2", priority="medium"),
        _rule(
            rule_id="r3",
            task_kind="k3",
            conditions=[
                {"scope_field": "lane", "operator": "equals", "value": "lane-x"}
            ],
            factors=["canonical_shapes"],
        ),
    ]
    results = {
        _evaluate(
            _fixture_profile(*order), node_archetype="compute", lane="lane-x"
        ).model_dump_json()
        for order in itertools.permutations(rules)
    }
    assert len(results) == 1


def _unvalidated_profile(
    scope_field: str, operator: object, logic: object = None
) -> ModelContextProfile:
    """A profile carrying a rule that validation would refuse, built unvalidated."""
    condition = ModelContextTaskKindCondition.model_construct(
        scope_field=scope_field, operator=operator, value="omni*", negate=False
    )
    fields = ModelContextTaskKindRule.model_fields
    rule = ModelContextTaskKindRule.model_construct(
        rule_id="bad",
        task_kind="kind_bad",
        priority=fields["priority"].default,
        condition_logic=logic
        if logic is not None
        else fields["condition_logic"].default,
        conditions=(condition,),
        factors=(F.EXEMPLAR,),
    )
    return _fixture_profile().model_copy(update={"task_kind_rules": (rule,)})


@pytest.mark.parametrize(
    ("scope_field", "operator", "logic", "message"),
    [
        ("repository", EnumContextTaskKindOperator.GLOB, None, "non-path field"),
        ("repository", "contains", None, "unknown operator"),
        (
            "prompt_text",
            EnumContextTaskKindOperator.EQUALS,
            None,
            "unknown caller-scope field",
        ),
        (
            "repository",
            EnumContextTaskKindOperator.EQUALS,
            "most",
            "unknown condition_logic",
        ),
    ],
)
def test_a_failed_evaluation_is_a_typed_error_not_a_silent_false(
    scope_field: str, operator: object, logic: object, message: str
) -> None:
    result = _evaluate(
        _unvalidated_profile(scope_field, operator, logic), repository="omnimarket"
    )
    assert result.status is EnumContextTaskKindRulesStatus.ERROR
    assert result.task_kind is None
    assert result.admitted_factors == ()
    assert result.error is not None
    assert message in result.error


# --- the per_prompt routes ported from file_path_routes.yaml ------------------


@pytest.mark.parametrize(
    ("repository", "path", "task_kind"),
    [
        ("omnidash", "server/consumers/kafka.ts", "kafka_consumer_conventions"),
        ("omnidash", "migrations/0001_init.sql", "migration_conventions"),
        ("omniclaude", "plugins/onex/hooks/pre_tool_use.py", "hook_conventions"),
        ("omnimarket", "tests/unit/test_x.py", "test_conventions"),
        ("omnimarket", "src/pkg/migrations/0001.sql", "migration_conventions"),
        # First match wins in the source table: tests is listed before migrations.
        ("omnimarket", "tests/migrations/test_m.py", "test_conventions"),
        # Repository-specific routes beat the generic ones, as their order did.
        ("omnidash", "server/consumers/tests/x.ts", "kafka_consumer_conventions"),
        ("omnimarket", "src/app.py", "unknown"),
    ],
)
def test_per_prompt_path_routes_keep_the_source_table_order(
    repository: str, path: str, task_kind: str
) -> None:
    result = _evaluate(
        _profile("per_prompt"),
        "per_prompt",
        repository=repository,
        request_paths=(path,),
    )
    assert result.status is EnumContextTaskKindRulesStatus.OK
    assert result.task_kind == task_kind
    expected = () if task_kind == "unknown" else (F.ARCHITECTURE_PATTERNS,)
    assert result.admitted_factors == expected


def test_per_prompt_with_no_descriptors_loads_nothing() -> None:
    result = _evaluate(_profile("per_prompt"), "per_prompt")
    assert result.task_kind == "unknown"
    assert result.admitted_factors == ()
    assert set(result.withheld_factors) == {
        F.ARCHITECTURE_PATTERNS,
        F.EXEMPLAR,
        F.PLAN_EXCERPTS,
    }


def test_per_prompt_plan_excerpts_load_only_for_a_cited_plan() -> None:
    result = _evaluate(
        _profile("per_prompt"),
        "per_prompt",
        cited_plan_paths=("beta/plans/2026-10-01-plan.md",),
    )
    assert result.task_kind == "plan_work"
    assert result.admitted_factors == (F.PLAN_EXCERPTS,)
