# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model choices of pricing, the lane dispatch brief builder and engine failover sit in declared config (OMN-21805)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from omnibase_core.validation.validator_contracts_and_di import ValidatorContractsAndDi

from omnimarket import pricing
from omnimarket.handlers.model_binding_reader import (
    model_binding,
    model_binding_section,
)
from omnimarket.models.lane_brief.enum_lane_engine import CLAUDE_LANE_NAMES
from omnimarket.nodes.node_lane_dispatch_intake_effect.handlers import (
    adapter_lane_dispatch as adapter,
)
from omnimarket.nodes.node_lane_dispatch_intake_effect.models.model_lane_dispatch_intake import (
    ModelPlacementRequest,
)
from omnimarket.nodes.node_lane_placement_recovery_effect.handlers import (
    handler_lane_engine_failover as failover,
)

SLICE_FILES = (
    "pricing.py",
    "nodes/node_lane_dispatch_intake_effect/handlers/adapter_lane_dispatch.py",
    "nodes/node_lane_placement_recovery_effect/handlers/handler_lane_engine_failover.py",
)


@pytest.mark.unit
def test_slice_has_no_literal_model_name_findings() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "omnimarket"
    paths = [root / name for name in SLICE_FILES]
    assert all(path.is_file() for path in paths), "a target file is missing"
    result = ValidatorContractsAndDi().validate(paths)
    assert result.metadata is not None, "the validator returned no scan metadata"
    assert result.metadata.files_processed == len(paths), "a target was not scanned"
    findings = [
        str(issue) for issue in result.issues if issue.rule_name == "literal_model_name"
    ]
    assert not findings, "\n".join(findings)


@pytest.mark.unit
def test_pricing_models_come_from_the_declared_bindings() -> None:
    baseline = model_binding("pricing_baseline_model")
    assert baseline
    assert baseline == pricing.DEFAULT_BASELINE_MODEL
    declared = tuple(model_binding_section("frontier_comparison_models"))
    assert declared
    assert len(set(declared)) == len(declared)
    assert declared == pricing.DEFAULT_FRONTIER_COMPARISON_MODELS


@pytest.mark.unit
def test_brief_model_is_declared_and_injected_into_the_builder(tmp_path: Path) -> None:
    assert model_binding("lane_dispatch_brief_model")
    seen: list[list[str]] = []

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        if argv[1].endswith("lane_rules_block.py"):
            return subprocess.CompletedProcess(argv, 0, "rules", "")
        if argv[2] == "constraints":
            Path(argv[argv.index("--out") + 1]).write_text("constraints\n")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    request = ModelPlacementRequest(
        lane="lane-a", ticket="OMN-10001", host="any", task="print the hostname"
    )
    adapter.ScriptBriefBuilder(
        Path("/plugin"), tmp_path, run=run, model="injected-model"
    ).build(request, "orch")
    constraints = next(argv for argv in seen if argv[2:3] == ["constraints"])
    assert constraints[constraints.index("--model") + 1] == "injected-model"
    assert not hasattr(adapter.ScriptBriefBuilder, "MODEL")


@pytest.mark.unit
def test_failover_claude_engines_keep_their_former_names() -> None:
    assert failover.CLAUDE is CLAUDE_LANE_NAMES
