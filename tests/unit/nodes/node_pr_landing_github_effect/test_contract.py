# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 and the seam registry (OMN-19826) for node_pr_landing_github_effect.

- AC3: the contract declares the existing ``GITHUB_TOKEN`` secret ref exactly as
  node_ci_rerun_effect does, and no other credential.
- The frozen names from the plan's seam registry: the node name, the three
  topics (registered in ``omnimarket.events.topics``) and the seven operations
  (contract 1.1.0 added read_pr_state, OMN-19831).
- Wired (wave-3 compose, OMN-19829, contract 1.2.0): the node has an
  ``onex.nodes`` entry point, consumes the request topic, publishes both result
  topics by class name, and routes the request to its handler. The
  orchestrator is the only other contract on those three topics.
"""

from __future__ import annotations

import importlib
import re
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events import topics
from omnimarket.nodes.node_pr_landing_github_effect.handlers import (
    HandlerPrLandingGithubEffect,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelGithubQuotaReading,
    ModelPrLandingGithubCompleted,
    ModelPrLandingGithubFailed,
    ModelPrLandingGithubRequest,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_NODES = _REPO_ROOT / "src" / "omnimarket" / "nodes"
_CONTRACT = _NODES / "node_pr_landing_github_effect" / "contract.yaml"
_CI_RERUN_CONTRACT = _NODES / "node_ci_rerun_effect" / "contract.yaml"
_ARM_CONTRACT = _NODES / "node_merge_sweep_auto_merge_arm_effect" / "contract.yaml"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

_REQUESTED = "onex.cmd.omnimarket.pr-landing-github-requested.v1"
_COMPLETED = "onex.evt.omnimarket.pr-landing-github-completed.v1"
_FAILED = "onex.evt.omnimarket.pr-landing-github-failed.v1"


def _load(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


# --- AC3 --------------------------------------------------------------------


def test_secrets_block_matches_node_ci_rerun_effect() -> None:
    ours = _load(_CONTRACT)["secrets"]
    theirs = _load(_CI_RERUN_CONTRACT)["secrets"]
    assert set(ours) == set(theirs) == {"GITHUB_TOKEN"}
    assert ours["GITHUB_TOKEN"]["required"] == theirs["GITHUB_TOKEN"]["required"]
    # The arm effect uses the same single ref.
    assert set(_load(_ARM_CONTRACT)["secrets"]) == {"GITHUB_TOKEN"}


def test_secrets_comparison_positive_control() -> None:
    """The comparison above must fail for a contract with a second credential."""
    ours = dict(_load(_CONTRACT)["secrets"])
    ours["OCC_APP_PRIVATE_KEY"] = {"description": "x", "required": True}
    assert set(ours) != set(_load(_CI_RERUN_CONTRACT)["secrets"])


def test_contract_names_no_other_credential_anywhere() -> None:
    text = _CONTRACT.read_text(encoding="utf-8")
    for forbidden in (
        r"\bresolve_occ_github_token\b",
        r"\bOCC_[A-Z_]*(TOKEN|KEY|SECRET)\b",
        r"\bPRIVATE_KEY\b",
        r"\bAPP_ID\b",
        r"onexbot-occ-writer",
        r"\bbypass\b",
    ):
        assert not re.search(forbidden, text), forbidden


# --- frozen names -----------------------------------------------------------


def test_contract_identity_and_topics() -> None:
    contract = _load(_CONTRACT)
    assert contract["name"] == "node_pr_landing_github_effect"
    assert contract["node_type"] == "EFFECT_GENERIC"
    assert contract["descriptor"]["node_archetype"] == "effect"
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [_REQUESTED]
    assert bus["publish_topics"] == [_COMPLETED, _FAILED]


def test_topics_are_registered_in_the_topic_registry() -> None:
    assert topics.PR_LANDING_GITHUB_REQUESTED_TOPIC_V1 == _REQUESTED
    assert topics.PR_LANDING_GITHUB_COMPLETED_TOPIC_V1 == _COMPLETED
    assert topics.PR_LANDING_GITHUB_FAILED_TOPIC_V1 == _FAILED


def test_only_the_effect_and_the_orchestrator_claim_the_topics() -> None:
    """The orchestrator sends the request and consumes both results; nobody else."""
    claims: dict[tuple[str, str], list[str]] = {}
    for path in sorted(_NODES.glob("*/contract.yaml")):
        bus = _load(path).get("event_bus") or {}
        for key in ("publish_topics", "subscribe_topics"):
            for topic in bus.get(key) or []:
                if topic in (_REQUESTED, _COMPLETED, _FAILED):
                    claims.setdefault((topic, key), []).append(path.parent.name)
    effect = "node_pr_landing_github_effect"
    orchestrator = "node_pr_landing_orchestrator"
    assert claims == {
        (_REQUESTED, "publish_topics"): [orchestrator],
        (_REQUESTED, "subscribe_topics"): [effect],
        (_COMPLETED, "publish_topics"): [effect],
        (_COMPLETED, "subscribe_topics"): [orchestrator],
        (_FAILED, "publish_topics"): [effect],
        (_FAILED, "subscribe_topics"): [orchestrator],
    }


def test_contract_declares_the_seven_operations_and_two_modes() -> None:
    contract = _load(_CONTRACT)
    assert [op["name"] for op in contract["operations"]] == [
        op.value for op in EnumPrLandingGithubOperation
    ]
    assert contract["modes"] == [m.value for m in EnumPrLandingGithubMode]
    assert [op.value for op in EnumPrLandingGithubOperation] == [
        "rerun_runs",
        "update_branch",
        "arm_auto_merge",
        "enqueue",
        "disarm",
        "read_head_checks",
        "read_pr_state",
    ]


def test_contract_version_is_bumped_for_the_wiring() -> None:
    """1.1.0 added read_pr_state (plan revision 1 section 5); 1.2.0 wires the bus."""
    contract = _load(_CONTRACT)
    assert contract["contract_version"] == {"major": 1, "minor": 2, "patch": 0}


def test_contract_models_resolve_to_the_seam_models() -> None:
    contract = _load(_CONTRACT)
    assert contract["input_model"]["name"] == ModelPrLandingGithubRequest.__name__
    assert contract["input_model"]["module"] == ModelPrLandingGithubRequest.__module__
    (route,) = contract["handler_routing"]["handlers"]
    assert route["event_model"] == {
        "name": ModelPrLandingGithubRequest.__name__,
        "module": ModelPrLandingGithubRequest.__module__,
    }
    # The runtime routes a returned model to its topic by class name, with the
    # leading Model stripped (handler_wiring's published_events resolver).
    results = {e["event_type"]: e["topic"] for e in contract["published_events"]}
    assert results == {
        ModelPrLandingGithubCompleted.__name__.removeprefix("Model"): _COMPLETED,
        ModelPrLandingGithubFailed.__name__.removeprefix("Model"): _FAILED,
    }
    for model in (
        ModelPrLandingGithubRequest,
        ModelPrLandingGithubCompleted,
        ModelPrLandingGithubFailed,
        ModelGithubQuotaReading,
    ):
        assert model.model_config.get("frozen") is True
        assert model.model_config.get("extra") == "forbid"


def test_every_result_carries_a_quota_reading_field() -> None:
    for model in (ModelPrLandingGithubCompleted, ModelPrLandingGithubFailed):
        assert "quota" in model.model_fields


def test_contract_is_scoped_to_the_dev_lane() -> None:
    assert _load(_CONTRACT)["runtime_lanes"] == ["compose-dev"]


# --- wired ------------------------------------------------------------------


def _pyproject_node_entry_points() -> set[str]:
    content = _PYPROJECT.read_text(encoding="utf-8")
    match = re.search(
        r'\[project\.entry-points\."onex\.nodes"\](.*?)(?=\n\[|\Z)', content, re.DOTALL
    )
    assert match is not None
    names = {
        line.split("=", 1)[0].strip()
        for line in match.group(1).splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    # Positive control: the table was actually read.
    assert "node_ci_rerun_effect" in names
    return names


def test_node_has_an_entry_point_so_discovery_wires_it() -> None:
    assert "node_pr_landing_github_effect" in _pyproject_node_entry_points()
    installed = {ep.name for ep in entry_points(group="onex.nodes")}
    assert "node_pr_landing_github_effect" in installed


def test_contract_routes_the_request_to_the_handler() -> None:
    """handler_routing names the handler class the runtime resolves and calls."""
    contract = _load(_CONTRACT)
    assert "lifecycle" not in contract
    assert "seam" not in contract
    (route,) = contract["handler_routing"]["handlers"]
    assert route["topic"] == _REQUESTED
    module = importlib.import_module(route["handler"]["module"])
    handler_cls = getattr(module, route["handler"]["name"])
    assert handler_cls is HandlerPrLandingGithubEffect


def test_contract_attaches_on_the_main_runtime_of_the_dev_lane() -> None:
    """The dev lane's effects runtime names no lane, so the scope sits on main."""
    contract = _load(_CONTRACT)
    assert contract["runtime_profiles"] == ["main"]
    assert "runtime_profiles" not in contract["descriptor"]
