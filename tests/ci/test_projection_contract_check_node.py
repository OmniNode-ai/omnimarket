# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Behaviour the original scripts did not have, and the node's canonical shape (OMN-20567)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.nodes.no_utcnow_check.model_source_file import ModelSourceFile
from omnibase_core.models.validation.model_validation_report import (
    ModelValidationReport,
)

from omnimarket.models.contract_projection_check import (
    EnumProjectionContractRule,
    ModelProjectionContractCheckInput,
    ModelProjectionNodeSources,
)
from omnimarket.nodes.node_contract_projection_check_compute.handlers.handler_projection_contract_check import (
    HandlerProjectionContractCheck,
)
from tests.ci.projection_contract_check_corpus import CASES
from tests.ci.projection_contract_check_runners import (
    NODE_MODULE,
    REPO_ROOT,
    materialize,
    run_node,
)

pytestmark = [pytest.mark.unit]

NODE_DIR = (
    REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_contract_projection_check_compute"
)


def _case(name: str):
    return next(case for case in CASES if case.name == name)


def test_contract_is_the_canonical_pure_compute_shape() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["node_archetype"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    assert set(contract["outputs"]) == {"report"}


def test_both_nodes_declare_their_command_and_terminal_topics() -> None:
    compute = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    effect = yaml.safe_load(
        (
            NODE_DIR.parent / "node_contract_projection_check_effect" / "contract.yaml"
        ).read_text(encoding="utf-8")
    )
    assert (
        compute["terminal_event"]
        == "onex.evt.omnimarket.contract-projection-checked.v1"
    )
    assert (
        effect["terminal_event"]
        == "onex.evt.omnimarket.contract-projection-gathered.v1"
    )
    assert compute["runtime_profiles"] == ["main"]
    assert effect["runtime_profiles"] == ["effects"]


def test_handler_returns_the_canonical_report_from_explicit_text() -> None:
    report = HandlerProjectionContractCheck().handle(
        ModelProjectionContractCheckInput(
            rule=EnumProjectionContractRule.DLQ,
            handlers=(
                ModelSourceFile(
                    path="src/omnimarket/nodes/node_projection_a/handlers/handler_a.py",
                    source="except ValidationError:\n    return None\n",
                ),
            ),
        )
    )
    assert isinstance(report, ModelValidationReport)
    assert report.overall_status == "FAIL"
    assert [f.rule_id for f in report.findings] == ["projection-dlq-path"]


def test_cursor_baseline_entry_that_now_passes_is_a_warning_not_a_failure() -> None:
    report = HandlerProjectionContractCheck().handle(
        ModelProjectionContractCheckInput(
            rule=EnumProjectionContractRule.CURSOR,
            nodes=(
                ModelProjectionNodeSources(
                    node="node_a",
                    contract_path="src/omnimarket/nodes/node_a/contract.yaml",
                    contract_text=(
                        "projection_api:\n  expose: true\n  table: t\n"
                        "  cursor_column: id\n  columns: [id]\n"
                    ),
                ),
            ),
            cursor_baseline=("node_a::t#0",),
        )
    )
    assert report.overall_status == "WARN"
    assert [(f.severity, f.rule_id) for f in report.findings] == [
        ("WARN", "projection-cursor-baseline-shrinkable")
    ]


@pytest.mark.parametrize("rule", ["access", "dlq", "cursor"])
def test_a_full_tree_run_that_scanned_nothing_is_an_error_never_a_pass(
    rule: str, tmp_path: Path
) -> None:
    (tmp_path / "src" / "omnimarket" / "nodes").mkdir(parents=True)
    proc = subprocess.run(
        [sys.executable, "-m", NODE_MODULE, "--rule", rule],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "gathered zero inputs" in proc.stderr


def test_unparseable_contract_is_an_error_for_access(tmp_path: Path) -> None:
    case = _case("access_clean_read_write")
    materialize(tmp_path, case)
    (tmp_path / "src/omnimarket/nodes/node_a/contract.yaml").write_text(
        "name: [unclosed\n", encoding="utf-8"
    )
    proc = subprocess.run(
        [sys.executable, "-m", NODE_MODULE, "--rule", "access"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert (
        "ERROR: cannot parse src/omnimarket/nodes/node_a/contract.yaml" in proc.stderr
    )


def test_unparseable_contract_is_skipped_with_a_warning_for_cursor(
    tmp_path: Path,
) -> None:
    case = _case("cursor_all_declared_clean")
    materialize(tmp_path, case)
    bad = tmp_path / "src/omnimarket/nodes/node_b/contract.yaml"
    bad.parent.mkdir(parents=True)
    bad.write_text("name: [unclosed\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", NODE_MODULE, "--rule", "cursor"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert (
        "warning: skipping unparseable src/omnimarket/nodes/node_b/contract.yaml"
        in proc.stderr
    )


def test_report_json_is_the_canonical_report(tmp_path: Path) -> None:
    case = _case("access_write_only_but_reads")
    materialize(tmp_path, case)
    report_path = tmp_path / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            NODE_MODULE,
            "--rule",
            "access",
            "--report-json",
            str(report_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    report = ModelValidationReport.model_validate(json.loads(report_path.read_text()))
    assert report.overall_status == "FAIL"
    assert report.findings[0].rule_id == "projection-contract-access"
    assert report.findings[0].location == "src/omnimarket/nodes/node_a/handler.py:6"


def test_run_node_helper_is_the_same_entrypoint_hooks_use(tmp_path: Path) -> None:
    observed = run_node(tmp_path, _case("access_clean_read_write"))
    assert observed["rc"] == 0
