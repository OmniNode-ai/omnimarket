# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The __main__ entrypoints of twenty-six nodes resolve their handler through the node's binding module."""

from pathlib import Path

import pytest
from omnibase_core.validation.validator_contracts_and_di import ValidatorContractsAndDi

_NODES_DIR = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_NODES = (
    "node_compliance_sweep",
    "node_contract_sweep",
    "node_coverage_sweep",
    "node_create_ticket",
    "node_dashboard_sweep",
    "node_data_flow_sweep",
    "node_database_sweep",
    "node_deep_dive_report_effect",
    "node_dependency_health_sweep",
    "node_deployment_fact_gate_effect",
    "node_dev_tag_pin_mover_compute",
    "node_dirty_canonical_sweep",
    "node_gap_compute",
    "node_harness_invocation_check_effect",
    "node_hook_chain_probe_effect",
    "node_internal_release_path_guard_compute",
    "node_lab_daemon_sync_effect",
    "node_lab_job_submit_effect",
    "node_local_review",
    "node_omniclaude_pin_bump_compute",
    "node_owner_summary_effect",
    "node_platform_diagnostics",
    "node_platform_readiness",
    "node_pr_title_check_compute",
    "node_private_hosted_minutes_watch_effect",
    "node_prrp_snapshot_publish_effect",
)
_RULE = "handler_hand_construction"


def _rules(path: Path) -> list[str]:
    result = ValidatorContractsAndDi().validate(path)
    return [str(issue.code) for issue in result.issues]


@pytest.mark.unit
@pytest.mark.parametrize("node", _NODES)
def test_main_entrypoint_holds_no_hand_construction(node: str) -> None:
    main = _NODES_DIR / node / "__main__.py"
    assert _RULE not in _rules(main)
    assert (_NODES_DIR / node / "container").is_dir()


@pytest.mark.unit
def test_checker_flags_an_inline_construction(tmp_path: Path) -> None:
    source = tmp_path / "__main__.py"
    source.write_text(
        "from omnimarket.nodes.node_gap_compute.handlers.handler_gap_compute import (\n"
        "    HandlerGapCompute,\n)\n\n\ndef main() -> object:\n"
        "    return HandlerGapCompute()\n"
    )
    assert _RULE in _rules(source)
