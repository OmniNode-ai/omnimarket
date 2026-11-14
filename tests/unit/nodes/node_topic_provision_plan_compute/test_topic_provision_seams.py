# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests: the provision plan agrees with the repo's contracts and profiles."""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_infra.runtime.runtime_profile import _PROFILES
from omnibase_infra.tools.contract_topic_extractor import ContractTopicExtractor

from omnimarket.models.topic_provision.enum_topic_provision import (
    EnumTopicProvisionMode,
)
from omnimarket.models.topic_provision.model_topic_provision_request import (
    ModelTopicProvisionRequest,
)
from omnimarket.models.topic_provision.model_topic_provision_rule import (
    ModelTopicProvisionRule,
)
from omnimarket.nodes.node_topic_provision_dry_run_effect.adapters.contract_facts import (
    read_contract_facts,
)
from omnimarket.nodes.node_topic_provision_plan_compute.handlers.handler_topic_provision_plan import (
    plan_topic_provision,
)

pytestmark = pytest.mark.unit

NODES_DIR = Path(__file__).parents[4] / "src" / "omnimarket" / "nodes"
NODE_DIR = NODES_DIR / "node_topic_provision_plan_compute"


def test_every_extracted_topic_is_in_the_facts() -> None:
    extracted = ContractTopicExtractor().extract(NODES_DIR)
    facts = read_contract_facts([NODES_DIR])
    fact_topics = {t for f in facts for t in f.topics}
    missing = sorted({e.topic for e in extracted} - fact_topics)
    assert not missing, f"topics missing from read_contract_facts: {missing}"
    command_topic = __import__("yaml").safe_load(
        (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )["runtime_dispatch"]["command_topic"]
    own = next(
        f for f in facts if f.contract_name == "node_topic_provision_plan_compute"
    )
    assert command_topic in own.topics


def test_every_named_profile_is_registered() -> None:
    facts = read_contract_facts([NODES_DIR])
    unknown = sorted({p for f in facts for p in f.runtime_profiles} - set(_PROFILES))
    assert not unknown, f"runtime profiles not registered: {unknown}"


def test_request_from_facts_plans_and_provisions_a_topic() -> None:
    facts = read_contract_facts([NODES_DIR])
    request = ModelTopicProvisionRequest(
        contracts=facts,
        profile="main",
        lane_manifest=tuple(f.contract_name for f in facts),
        rule=ModelTopicProvisionRule(mode=EnumTopicProvisionMode.PROFILE),
    )
    plan = plan_topic_provision(request)
    assert len(plan.topics_to_provision) >= 1
