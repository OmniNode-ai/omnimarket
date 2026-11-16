# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged OMN-20172 shrink-retry caps are declared on their typed model.

Red: the deployment-fact gate reported every packaged ``escalation_policy``
shrink key as an undeclared key because ``ModelTaskClassEscalationPolicy``
declared only ``max_escalations``, ``paid_from_size_band`` and ``tier_order``.
Green: the caps and the ``shrink_unit_prompt`` wording are typed fields on the
model — declared, so reported by no one — and never marked as deployment
facts, because a cap and a prompt wording are routing behavior, not a
deployment's host, model, credential or vendor.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.inference.task_class_authority import ModelTaskClassAuthority
from omnimarket.models.delegation.model_deployment_fact_marker import (
    undeclared_key_paths,
)
from omnimarket.nodes.node_deployment_fact_gate_effect.handlers.handler_deployment_fact_gate import (
    PACKAGED_CONFIG_DIR,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONFIGS = _REPO_ROOT / PACKAGED_CONFIG_DIR
_CONTRACTS = _CONFIGS / "task_class_contracts.v1.yaml"


def _raw_contracts() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACTS.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


class TestShrinkCapsAreDeclared:
    def test_packaged_task_class_contracts_declare_every_escalation_policy_key(
        self,
    ) -> None:
        raw = _raw_contracts()
        undeclared = undeclared_key_paths(raw, ModelTaskClassAuthority)
        assert not [path for path in undeclared if "escalation_policy" in path]

    def test_shrink_caps_load_with_their_types(self) -> None:
        authority = ModelTaskClassAuthority.model_validate(_raw_contracts())
        declared = [
            (name, entry.escalation_policy)
            for name, entry in authority.task_classes.items()
            if entry.escalation_policy is not None
            and entry.escalation_policy.max_config_swaps is not None
        ]
        assert declared, "no packaged class declares max_config_swaps"
        for _name, policy in declared:
            assert isinstance(policy.max_split_depth, int)
            assert isinstance(policy.unit_attempts_per_unit, int)
            assert isinstance(policy.shrink_unit_prompt, str)
            assert "{source}" in policy.shrink_unit_prompt

    def test_named_break_an_undeclared_cap_key_is_reported(self) -> None:
        raw = _raw_contracts()
        head = copy.deepcopy(raw)
        policy = next(
            entry["escalation_policy"]
            for entry in head["task_classes"].values()
            if "escalation_policy" in entry
        )
        policy["an_undeclared_cap"] = 3
        undeclared = undeclared_key_paths(head, ModelTaskClassAuthority)
        assert any(
            path.endswith("escalation_policy.an_undeclared_cap") for path in undeclared
        )
