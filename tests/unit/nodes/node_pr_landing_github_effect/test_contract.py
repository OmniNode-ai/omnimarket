# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 and the seam registry (OMN-19826) for node_pr_landing_github_effect.

- AC3: the contract declares the existing ``GITHUB_TOKEN`` secret ref exactly as
  node_ci_rerun_effect does, and no other credential.
- The frozen names from the plan's seam registry: the node name, the three
  topics (registered in ``omnimarket.events.topics``) and the six operations.
- Not wired: runtime discovery (omnibase_infra ``runtime/auto_wiring/discovery.py``)
  walks only the ``onex.nodes`` entry points, so a node with no entry point and
  no handler cannot be subscribed or dispatched.
"""

from __future__ import annotations

import re
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events import topics
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
    seam = contract["seam"]
    assert seam["command_topic"] == _REQUESTED
    assert [t["topic"] for t in seam["result_topics"]] == [_COMPLETED, _FAILED]


def test_topics_are_registered_in_the_topic_registry() -> None:
    assert topics.PR_LANDING_GITHUB_REQUESTED_TOPIC_V1 == _REQUESTED
    assert topics.PR_LANDING_GITHUB_COMPLETED_TOPIC_V1 == _COMPLETED
    assert topics.PR_LANDING_GITHUB_FAILED_TOPIC_V1 == _FAILED


def test_no_contract_claims_the_result_topics_on_the_bus_yet() -> None:
    """The names are owned here; no contract publishes them before the handler."""
    owners: dict[str, list[str]] = {_REQUESTED: [], _COMPLETED: [], _FAILED: []}
    for path in sorted(_NODES.glob("*/contract.yaml")):
        bus = _load(path).get("event_bus") or {}
        for key in ("publish_topics", "subscribe_topics"):
            for topic in bus.get(key) or []:
                if topic in owners:
                    owners[topic].append(path.parent.name)
    assert owners == {_REQUESTED: [], _COMPLETED: [], _FAILED: []}


def test_contract_declares_the_six_operations_and_two_modes() -> None:
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
    ]


def test_contract_models_resolve_to_the_seam_models() -> None:
    contract = _load(_CONTRACT)
    assert contract["input_model"]["name"] == ModelPrLandingGithubRequest.__name__
    assert contract["input_model"]["module"] == ModelPrLandingGithubRequest.__module__
    results = {t["event_type"]: t["module"] for t in contract["seam"]["result_topics"]}
    assert results == {
        ModelPrLandingGithubCompleted.__name__: ModelPrLandingGithubCompleted.__module__,
        ModelPrLandingGithubFailed.__name__: ModelPrLandingGithubFailed.__module__,
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


# --- not wired --------------------------------------------------------------


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


def test_node_has_no_entry_point_so_discovery_cannot_wire_it() -> None:
    assert "node_pr_landing_github_effect" not in _pyproject_node_entry_points()
    installed = {ep.name for ep in entry_points(group="onex.nodes")}
    assert "node_pr_landing_github_effect" not in installed


def test_contract_declares_no_handler_until_wave_two() -> None:
    contract = _load(_CONTRACT)
    assert "handler" not in contract
    assert "handler_routing" not in contract
    assert contract["lifecycle"] == "experimental"
    assert contract["seam"]["wired_by"] == "OMN-19831"


def test_contract_declares_no_runtime_bus_surface() -> None:
    """No key any discovery path or the topic graph reads as a subscription.

    A package contract scan subscribes any contract with
    event_bus.subscribe_topics, entry point or not, so the seam keeps its topic
    names out of every runtime-read key until the handler lands.
    """
    contract = _load(_CONTRACT)
    for key in (
        "event_bus",
        "runtime_dispatch",
        "published_events",
        "consumed_events",
        "subscribed_events",
        "terminal_event",
        "topics",
        "subscriptions",
    ):
        assert key not in contract, key
