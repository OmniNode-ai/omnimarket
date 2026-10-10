# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployment-fact gate refuses new deployment facts in packaged routing configs (OMN-20287).

Red: omnimarket#3624's additions to the three packaged configs. Green: the
packaged configs in the tree against themselves as the base. Shrink-only: the
baseline is the base revision, so a removed value cannot come back, a value
cannot gain an occurrence, and a value moved to a new field is new there.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.enums.enum_deployment_fact_kind import EnumDeploymentFactKind
from omnimarket.inference.task_class_authority import ModelTaskClassAuthority
from omnimarket.models.delegation.model_deployment_fact_marker import (
    NEUTRAL_LOCAL_ONLY_DEFAULTS,
    deployment_fact_fields,
    is_neutral,
)
from omnimarket.models.delegation.model_packaged_routing_tiers import (
    ModelPackagedRoutingTiers,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelBifrostDelegationConfig,
)
from omnimarket.nodes.node_deployment_fact_gate_effect.__main__ import main
from omnimarket.nodes.node_deployment_fact_gate_effect.handlers.handler_deployment_fact_gate import (
    PACKAGED_CONFIG_DIR,
    PACKAGED_ROUTING_CONFIGS,
    DeploymentFactGateError,
    judge_packaged_configs,
)
from omnimarket.nodes.node_deployment_fact_gate_effect.models import (
    ModelDeploymentFactGateResult,
    ModelPackagedConfigTexts,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONFIGS = _REPO_ROOT / PACKAGED_CONFIG_DIR
_PR3624 = (
    _REPO_ROOT / "tests/fixtures/deployment_fact_gate/pr3624_config_additions.yaml"
)


def _tree() -> dict[str, dict[str, Any]]:
    return {
        name: yaml.safe_load((_CONFIGS / name).read_text(encoding="utf-8"))
        for name in PACKAGED_ROUTING_CONFIGS
    }


def _judge(
    base: dict[str, dict[str, Any]], head: dict[str, dict[str, Any]]
) -> ModelDeploymentFactGateResult:
    return judge_packaged_configs(
        [
            ModelPackagedConfigTexts(
                file_name=name,
                base_text=yaml.safe_dump(base[name]),
                head_text=yaml.safe_dump(head[name]),
            )
            for name in PACKAGED_ROUTING_CONFIGS
        ],
        "base",
    )


def _with_pr3624(tree: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    additions = yaml.safe_load(_PR3624.read_text(encoding="utf-8"))
    head = copy.deepcopy(tree)
    head["bifrost_delegation.yaml"]["backends"].extend(
        additions["bifrost_delegation.yaml"]["backends_appended"]
    )
    head["routing_tiers.yaml"]["harness_tiers"] = additions["routing_tiers.yaml"][
        "harness_tiers"
    ]
    chains = additions["task_class_contracts.v1.yaml"]["escalation_chain_by_class"]
    for name, chain in chains.items():
        head["task_class_contracts.v1.yaml"]["task_classes"][name][
            "escalation_chain"
        ] = chain
    return head


class TestMarkedFields:
    def test_every_named_deployment_fact_field_is_marked(self) -> None:
        marked = {
            (name, field.dotted, field.kind)
            for name, model in PACKAGED_ROUTING_CONFIGS.items()
            for field in deployment_fact_fields(model)
        }
        kind = EnumDeploymentFactKind
        assert marked == {
            ("bifrost_delegation.yaml", "backends[].backend_id", kind.BACKEND),
            ("bifrost_delegation.yaml", "backends[].provider", kind.PROVIDER),
            ("bifrost_delegation.yaml", "backends[].endpoint_url", kind.ENDPOINT),
            ("bifrost_delegation.yaml", "backends[].model_name", kind.MODEL_NAME),
            ("bifrost_delegation.yaml", "backends[].secret_ref", kind.SECRET_REF),
            ("bifrost_delegation.yaml", "backends[].api_key_ref", kind.SECRET_REF),
            ("bifrost_delegation.yaml", "backends[].api_key_env", kind.SECRET_REF),
            (
                "bifrost_delegation.yaml",
                "routing_rules[].backend_ids",
                kind.ROUTING_ORDER,
            ),
            ("bifrost_delegation.yaml", "default_backends", kind.ROUTING_ORDER),
            ("routing_tiers.yaml", "tiers[].name", kind.ROUTING_TIER),
            ("routing_tiers.yaml", "tiers[].models[].id", kind.MODEL_NAME),
            ("routing_tiers.yaml", "tiers[].models[].backend_id", kind.BACKEND),
            ("routing_tiers.yaml", "tiers[].eval_model", kind.MODEL_NAME),
            (
                "task_class_contracts.v1.yaml",
                "task_classes.{}.escalation_policy.tier_order",
                kind.ROUTING_ORDER,
            ),
            ("task_class_contracts.v1.yaml", "default_task_model_ref", kind.MODEL_NAME),
            ("task_class_contracts.v1.yaml", "task_model_overrides", kind.MODEL_NAME),
        }

    def test_the_typed_models_still_load_the_packaged_files(self) -> None:
        tree = _tree()
        ModelPackagedRoutingTiers.model_validate(tree["routing_tiers.yaml"])
        authority = ModelTaskClassAuthority.model_validate(
            tree["task_class_contracts.v1.yaml"]
        )
        assert authority.default_task_model_ref is not None
        assert ModelBifrostDelegationConfig.model_fields["backends"] is not None

    def test_neutral_set_is_local_only(self) -> None:
        assert is_neutral(EnumDeploymentFactKind.BACKEND, "local-coder")
        assert is_neutral(EnumDeploymentFactKind.ROUTING_ORDER, "local")
        assert not is_neutral(EnumDeploymentFactKind.ROUTING_ORDER, "local > claude")
        assert not is_neutral(EnumDeploymentFactKind.MODEL_NAME, "Qwen3.8-27B")
        assert (
            NEUTRAL_LOCAL_ONLY_DEFAULTS[EnumDeploymentFactKind.SECRET_REF]
            == frozenset()
        )


class TestRedOnPr3624:
    def test_pr3624_additions_are_refused(self) -> None:
        tree = _tree()
        result = _judge(tree, _with_pr3624(tree))

        assert not result.passed
        new_backends = {
            f.value
            for f in result.new_facts
            if f.kind is EnumDeploymentFactKind.BACKEND
        }
        assert new_backends == {
            "harness-codex",
            "harness-claude-glm-flash",
            "harness-claude-glm",
            "harness-claude-haiku",
            "harness-claude-sonnet",
            "harness-claude-opus",
        }
        new_models = {
            f.value
            for f in result.new_facts
            if f.kind is EnumDeploymentFactKind.MODEL_NAME
        }
        assert {
            "claude-haiku-5-5",
            "claude-sonnet-5-5",
            "claude-opus-5-5",
        } <= new_models
        assert {(k.file_name, k.path) for k in result.new_undeclared_keys} == {
            ("bifrost_delegation.yaml", "backends[].kind"),
            ("bifrost_delegation.yaml", "backends[].harness"),
            ("bifrost_delegation.yaml", "backends[].surface"),
            ("bifrost_delegation.yaml", "backends[].tenant_scope"),
            ("routing_tiers.yaml", "harness_tiers"),
            ("task_class_contracts.v1.yaml", "task_classes.{}.escalation_chain"),
        }


class TestGreenOnTheTree:
    def test_tree_against_itself_passes(self) -> None:
        tree = _tree()
        result = _judge(tree, copy.deepcopy(tree))
        assert result.passed
        assert result.inventory, "the tree still carries deployment facts to move"

    def test_removing_a_fact_passes(self) -> None:
        tree = _tree()
        head = copy.deepcopy(tree)
        head["task_class_contracts.v1.yaml"].pop("task_model_overrides")
        assert _judge(tree, head).passed

    def test_a_neutral_local_backend_passes(self) -> None:
        tree = _tree()
        head = copy.deepcopy(tree)
        local = next(
            b
            for b in head["bifrost_delegation.yaml"]["backends"]
            if b["backend_id"] == "local-coder"
        )
        head["bifrost_delegation.yaml"]["backends"].remove(local)
        head["bifrost_delegation.yaml"]["backends"].append(local)
        assert _judge(tree, head).passed

    def test_the_cli_passes_on_this_checkout_against_head(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--repo-root", str(_REPO_ROOT), "--base", "HEAD"]) == 0


class TestBaselineCannotGrow:
    def test_a_removed_fact_cannot_come_back(self) -> None:
        tree = _tree()
        shrunk = copy.deepcopy(tree)
        shrunk["task_class_contracts.v1.yaml"].pop("default_task_model_ref")
        assert _judge(tree, shrunk).passed
        result = _judge(shrunk, tree)
        assert [(f.path, f.value) for f in result.new_facts] == [
            ("default_task_model_ref", "Qwen3.8-27B")
        ]

    def test_an_existing_value_cannot_gain_an_occurrence(self) -> None:
        tree = _tree()
        head = copy.deepcopy(tree)
        head["task_class_contracts.v1.yaml"]["task_model_overrides"]["document"] = (
            "Qwen3.8-27B"
        )
        result = _judge(tree, head)
        assert [(f.path, f.base_count, f.head_count) for f in result.new_facts] == [
            ("task_model_overrides.document", 0, 1)
        ]

    def test_a_reordered_class_ladder_is_a_new_fact(self) -> None:
        tree = _tree()
        head = copy.deepcopy(tree)
        policy = head["task_class_contracts.v1.yaml"]["task_classes"]["planning"][
            "escalation_policy"
        ]
        policy["tier_order"] = ["local", "claude"]
        result = _judge(tree, head)
        assert [(f.path, f.value) for f in result.new_facts] == [
            ("task_classes.planning.escalation_policy.tier_order", "local > claude")
        ]

    def test_a_vendor_backend_cannot_be_added(self) -> None:
        tree = _tree()
        head = copy.deepcopy(tree)
        head["bifrost_delegation.yaml"]["backends"].append(
            {
                "backend_id": "cloud-new-vendor",
                "provider": "newvendor",
                "endpoint_url": "https://api.example.invalid/v1/chat/completions",
                "model_name": "new-model",
                "secret_ref": "llm.newvendor.api_key",
                "tier": "frontier_api",
            }
        )
        kinds = {f.kind for f in _judge(tree, head).new_facts}
        assert kinds == {
            EnumDeploymentFactKind.BACKEND,
            EnumDeploymentFactKind.PROVIDER,
            EnumDeploymentFactKind.ENDPOINT,
            EnumDeploymentFactKind.MODEL_NAME,
            EnumDeploymentFactKind.SECRET_REF,
        }


class TestFailsClosed:
    def test_a_head_file_that_is_not_a_mapping_raises(self) -> None:
        with pytest.raises(DeploymentFactGateError):
            judge_packaged_configs(
                [
                    ModelPackagedConfigTexts(
                        file_name="routing_tiers.yaml",
                        base_text=None,
                        head_text="- x\n",
                    )
                ],
                "base",
            )

    def test_an_unknown_file_raises(self) -> None:
        with pytest.raises(DeploymentFactGateError):
            judge_packaged_configs(
                [
                    ModelPackagedConfigTexts(
                        file_name="other.yaml", base_text=None, head_text="a: 1\n"
                    )
                ],
                "base",
            )

    def test_an_unresolvable_base_exits_2(self) -> None:
        assert (
            main(
                ["--repo-root", str(_REPO_ROOT), "--base", "no-such-revision-omn20287"]
            )
            == 2
        )

    def test_no_baseline_reports_every_fact(self) -> None:
        assert main(["--repo-root", str(_REPO_ROOT), "--no-base"]) == 1


class TestHostileReviewerPromptContract:
    def test_pr_review_prompt_makes_a_packaged_deployment_fact_blocking(self) -> None:
        from omnimarket.review.prompt_builder import (
            ModelPromptBuilderInput,
            build_prompt,
        )

        prompt = build_prompt(
            ModelPromptBuilderInput(
                prompt_template_id="adversarial_reviewer_pr",
                context_content="diff --git a/x b/x",
                model_context_window=32768,
            )
        ).user_prompt
        assert (
            "A deployment fact added to packaged or shipped config (a backend, an "
            "endpoint host or port, a model name, a secret reference, a provider "
            "choice, or a per-class routing order) is a blocking finding of "
            "severity critical: routing decisions belong in a deployment overlay, "
            "not in the config the package ships."
        ) in prompt


class TestGateWiring:
    def test_hook_ci_job_and_required_context_are_wired(self) -> None:
        config = yaml.safe_load(
            (_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        )
        hooks = [
            hook
            for repo in config["repos"]
            for hook in repo["hooks"]
            if hook["id"] == "deployment-fact-gate"
        ]
        module = "omnimarket.nodes.node_deployment_fact_gate_effect"
        assert [hook["entry"] for hook in hooks] == [f"uv run python -m {module}"]

        workflow = yaml.safe_load(
            (_REPO_ROOT / ".github/workflows/deployment-fact-gate.yml").read_text(
                encoding="utf-8"
            )
        )
        job = workflow["jobs"]["deployment-fact-gate"]
        assert job["name"] == "Deployment Fact Gate"
        assert any(module in str(step.get("run", "")) for step in job["steps"])

        from scripts.ci.ci_summary_gate import EXPECTED_EXTERNAL_CONTEXTS

        assert "Deployment Fact Gate" in EXPECTED_EXTERNAL_CONTEXTS
        merge_group = yaml.safe_load(
            (
                _REPO_ROOT / ".github/workflows/merge-group-pr-scoped-contexts.yml"
            ).read_text(encoding="utf-8")
        )
        assert merge_group["jobs"]["ctx-deployment-fact-gate"]["name"] == (
            "Deployment Fact Gate"
        )
