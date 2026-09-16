# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A duplicate local-ingress alias fails at PR time, not at runtime boot (OMN-17888).

WHAT THIS GATE EXISTS FOR
-------------------------
``omnibase_infra`` ``runtime_local_ingress.py`` keys one local-ingress alias per
``handler_routing`` entry on that entry's ``operation``, and refuses a second registration
of the same alias unless the two routes expose the same interface. On a collision it raises
``ValueError: Duplicate local ingress route alias ...`` — at RUNTIME BOOT.

On 2026-09-16 a contract change that passed every gate in both repositories made
``node_redeploy_deploy_effect`` declare one operation twice with two different input models.
``omninode-runtime`` crash-looped, ``:8085/ready`` refused, the compose-dev lab-pass receipt
FAILed on ``ready_main`` and ``health_dimensions``, and delivery to staging is fail-closed
on that receipt, so the dev lane, staging delivery and both compose lanes were down for an
afternoon. Nothing at PR time asked the question; this is the gate that does.

RED FIRST, AGAINST THE REAL PRE-IMAGE
-------------------------------------
The mutant below is not invented: it is the contract as it stood at ``f575e956``, the squash
that caused the outage, with both entries under ``redeploy.deploy.publish_monitor``. Run
against that shape the validator exits 1 and reports both halves; run against the tree as it
stands it exits 0 over 2,486 aliases from 403 contracts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.validators.local_ingress_alias_collision import (
    ModelLocalIngressAliasFinding,
    _operation_input_model_conflicts,
    collect_findings,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NODES = _REPO_ROOT / "src" / "omnimarket" / "nodes"
_CONTRACT = _NODES / "node_redeploy_deploy_effect" / "contract.yaml"

_COLLIDING_OPERATION = "redeploy.deploy.publish_monitor"

# Floors, both of them positive controls rather than thresholds: a scan that collapses
# reports zero findings and reads exactly like a clean tree.
_MIN_ALIASES = 500
_MIN_CONTRACTS = 300


def test_the_tree_builds_a_local_ingress_alias_table_with_no_collision() -> None:
    """The live invariant: every contract in the tree can be booted together."""
    findings, alias_count, contracts_read = collect_findings()

    assert alias_count >= _MIN_ALIASES, (
        f"the alias table collapsed to {alias_count}; this assertion would be vacuous"
    )
    assert contracts_read >= _MIN_CONTRACTS, (
        f"the contract read collapsed to {contracts_read}; this would be vacuous"
    )
    assert not findings, "\n".join(finding.render() for finding in findings)


def test_the_gate_fires_on_the_contract_that_caused_the_outage(tmp_path: Path) -> None:
    """Mutation control: the f575e956 pre-image, reported with both models named.

    The static half is exercised against a throwaway tree, because the boot half resolves
    contracts from the INSTALLED package root and cannot be pointed at one. The boot half's
    own red is recorded in the module docstring and was reproduced against this same shape.
    """
    contract: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text())
    for entry in contract["handler_routing"]["handlers"]:
        entry["operation"] = _COLLIDING_OPERATION

    node_dir = tmp_path / "node_redeploy_deploy_effect"
    node_dir.mkdir(parents=True)
    (node_dir / "contract.yaml").write_text(yaml.safe_dump(contract, sort_keys=False))

    findings, contracts_read = _operation_input_model_conflicts(tmp_path)

    assert contracts_read == 1, f"the mutant tree was not read: {contracts_read}"
    assert len(findings) == 1, f"expected exactly one finding, got {findings}"
    only = findings[0]
    assert isinstance(only, ModelLocalIngressAliasFinding)
    assert only.kind == "operation_input_model_conflict"
    assert only.operation == _COLLIDING_OPERATION
    assert only.contract == "node_redeploy_deploy_effect"
    assert "ModelDeployPublishCommand" in only.detail
    assert "ModelDeployRebuildCompleted" in only.detail


def test_the_gate_does_not_sweep_up_a_shared_operation_that_is_legal(
    tmp_path: Path,
) -> None:
    """Falsification control on SCOPE, using a real contract rather than a fixture.

    ``node_redeploy_orchestrator`` routes five entries under ONE operation and boots fine,
    because none declares a per-entry ``input_model``, so every route it builds is
    equivalent and the registry deduplicates them deliberately. A gate that reported it
    would be banning the pattern rather than the defect.
    """
    sibling = yaml.safe_load(
        (_NODES / "node_redeploy_orchestrator" / "contract.yaml").read_text()
    )
    entries = sibling["handler_routing"]["handlers"]

    # Positive control on the read: the sibling really does share one operation.
    assert len(entries) > 1
    assert len({entry["operation"] for entry in entries}) == 1

    node_dir = tmp_path / "node_redeploy_orchestrator"
    node_dir.mkdir(parents=True)
    (node_dir / "contract.yaml").write_text(yaml.safe_dump(sibling, sort_keys=False))

    findings, contracts_read = _operation_input_model_conflicts(tmp_path)

    assert contracts_read == 1
    assert findings == ()


def test_an_empty_tree_is_a_broken_scan_not_a_clean_one(tmp_path: Path) -> None:
    """Fail-closed: zero contracts read must never be reported as zero findings."""
    findings, contracts_read = _operation_input_model_conflicts(tmp_path)

    assert findings == ()
    assert contracts_read == 0, (
        "an empty scan must be distinguishable from a clean one; main() exits 2 on this"
    )
