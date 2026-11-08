# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22892: the generator builds cases that each name a break, and refuses declarations that cannot fail.

Golden: a declaration with named breaks and kept red receipts yields one generated case per
named break, and the omnibase_core validator CLI exits 0 on the node's output directory.
Error: a case that names no break, and a case whose named break stayed green in the kept
receipts, each make the node raise ChainScenarioDeclarationError, and the same output
directory makes the validator CLI exit 1. The validator CLI is run exactly as omnibase_core's
pre-commit hook runs it, so a green here is the validator's own verdict, not a copy of its rules.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_event_chain_generator_compute.handlers.handler_scenario_test_generator import (
    HandlerScenarioTestGenerator,
)
from omnimarket.nodes.node_event_chain_generator_compute.models.model_chain_scenario_declaration_error import (
    ChainScenarioDeclarationError,
)
from omnimarket.nodes.node_event_chain_generator_compute.models.model_scenario_test_generation import (
    ModelScenarioTestGenerationRequest,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parent / "fixtures"
_NODE = "node_fixture_record_effect"
_VALIDATOR_ENTRY = (
    "omnibase_core.nodes.node_validation_report_write_effect.runtime_validator_run"
)
_VALIDATOR_CLASS = (
    "omnibase_core.validation.validator_chain_scenario:ValidatorChainScenario"
)


def _declaration(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (_FIXTURES / name / "declaration.yaml").read_text(encoding="utf-8")
    )
    return loaded


def _receipts(name: str) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(
        (_FIXTURES / name / "kept_receipts.json").read_text(encoding="utf-8")
    )
    return loaded


def _request(name: str) -> ModelScenarioTestGenerationRequest:
    return ModelScenarioTestGenerationRequest.model_validate(
        {
            "node_name": _NODE,
            "contract_document": _declaration(name),
            "kept_receipts": _receipts(name),
        }
    )


def _output_dir(tmp_path: Path, name: str, generated_test: str | None) -> Path:
    """The node's output directory: its contract, its kept receipts and, when generated, its test."""
    out = tmp_path / name
    out.mkdir()
    shutil.copy(_FIXTURES / name / "declaration.yaml", out / "contract.yaml")
    shutil.copy(
        _FIXTURES / name / "kept_receipts.json", out / "chain_scenario_receipts.json"
    )
    if generated_test is not None:
        (out / "tests").mkdir()
        (out / "tests" / "test_chain_scenarios.py").write_text(
            generated_test, encoding="utf-8"
        )
    return out


def _validator_cli(directory: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, "-m", _VALIDATOR_ENTRY, _VALIDATOR_CLASS, str(directory)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_golden_every_generated_case_names_a_break_and_the_validator_exits_zero(
    tmp_path: Path,
) -> None:
    result = HandlerScenarioTestGenerator().handle(_request("clean"))

    declared = {
        (case["case_name"], named)
        for case in _declaration("clean")["chain_scenarios"]["cases"]
        for named in case["breaks"]
    }
    generated = {(c.case_name, c.named_break) for c in result.generated_cases}
    assert result.status == "ok"
    assert generated == declared
    assert generated, "a generator that yields no case would pass vacuously"
    assert all(c.named_break for c in result.generated_cases)
    assert {c.kind for c in result.generated_cases} == {"GOLDEN", "ERROR"}

    verdict = _validator_cli(_output_dir(tmp_path, "clean", result.test_source))
    assert verdict.returncode == 0, verdict.stdout + verdict.stderr


@pytest.mark.parametrize(
    ("fixture", "rule_id"),
    [
        ("violation_case_without_named_breaks", "case_without_named_breaks"),
        ("violation_generated_test_cannot_fail", "generated_test_cannot_fail"),
    ],
)
def test_error_a_case_that_cannot_fail_raises_and_the_validator_exits_one(
    tmp_path: Path, fixture: str, rule_id: str
) -> None:
    with pytest.raises(ChainScenarioDeclarationError) as raised:
        HandlerScenarioTestGenerator().handle(_request(fixture))
    assert rule_id in raised.value.rule_ids
    assert raised.value.node_name == _NODE

    verdict = _validator_cli(_output_dir(tmp_path, fixture, None))
    assert verdict.returncode == 1, verdict.stdout + verdict.stderr
    assert rule_id in verdict.stdout + verdict.stderr


def test_the_node_judges_receipts_only_when_it_is_given_them() -> None:
    """Without receipts the receipts rule cannot be judged, so the same declaration generates."""
    request = ModelScenarioTestGenerationRequest.model_validate(
        {
            "node_name": _NODE,
            "contract_document": _declaration("violation_generated_test_cannot_fail"),
        }
    )
    assert HandlerScenarioTestGenerator().handle(request).status == "ok"
