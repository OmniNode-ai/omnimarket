# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for contract_secret_ref (OMN-12856).

Verifies:
(a) Resolution from a declared secret succeeds and returns the ref-name.
(b) Missing secrets block raises ValueError with a clear message naming the ref.
(c) Declared-but-absent secret name raises ValueError naming the secret.
(d) The six affected GitHub nodes (node_ci_fix_effect, node_ci_rerun_effect,
    node_linear_triage, node_merge_sweep_auto_merge_arm_effect,
    node_merge_sweep_compute, node_verification_receipt_generator)
    all declare GITHUB_TOKEN in their contracts, so contract_secret_ref returns
    'GITHUB_TOKEN' for each.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.contract_topics import contract_secret_ref

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NODES_ROOT = _REPO_ROOT / "src" / "omnimarket" / "nodes"

# All nodes updated by OMN-12856 to declare GITHUB_TOKEN in their contracts.
_GITHUB_TOKEN_NODES: tuple[str, ...] = (
    "node_ci_fix_effect",
    "node_ci_rerun_effect",
    "node_linear_triage",
    "node_merge_sweep_auto_merge_arm_effect",
    "node_merge_sweep_compute",
    "node_verification_receipt_generator",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_contract(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "contract.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# (a) Resolution succeeds for a declared secret
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_secret_ref_returns_ref_name(tmp_path: Path) -> None:
    """contract_secret_ref returns the ref-name (= dict key) for a declared secret."""
    contract = _make_contract(
        tmp_path,
        """
        name: test_node
        secrets:
          GITHUB_TOKEN:
            description: "GitHub PAT"
            required: true
        event_bus:
          subscribe_topics:
            - onex.cmd.test.v1
          publish_topics:
            - onex.evt.test.v1
        """,
    )
    ref = contract_secret_ref(contract, "GITHUB_TOKEN")
    assert ref == "GITHUB_TOKEN"


# ---------------------------------------------------------------------------
# (b) Missing secrets block raises ValueError naming the ref
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_secret_ref_missing_secrets_block_raises(tmp_path: Path) -> None:
    """When the contract has no 'secrets' block, ValueError names the secret."""
    contract = _make_contract(
        tmp_path,
        """
        name: test_node
        event_bus:
          subscribe_topics:
            - onex.cmd.test.v1
          publish_topics:
            - onex.evt.test.v1
        """,
    )
    with pytest.raises(ValueError, match="secrets"):
        contract_secret_ref(contract, "GITHUB_TOKEN")


# ---------------------------------------------------------------------------
# (c) Declared block but absent key raises ValueError naming the secret
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_secret_ref_missing_key_raises(tmp_path: Path) -> None:
    """When the secrets block exists but the key is absent, ValueError names it."""
    contract = _make_contract(
        tmp_path,
        """
        name: test_node
        secrets:
          SOME_OTHER_TOKEN:
            description: "other"
            required: false
        event_bus:
          subscribe_topics:
            - onex.cmd.test.v1
          publish_topics:
            - onex.evt.test.v1
        """,
    )
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        contract_secret_ref(contract, "GITHUB_TOKEN")


# ---------------------------------------------------------------------------
# (d) All OMN-12856 GitHub nodes declare GITHUB_TOKEN in their live contracts
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("node_name", _GITHUB_TOKEN_NODES)
def test_node_contract_declares_github_token(node_name: str) -> None:
    """Each OMN-12856-updated node contract declares GITHUB_TOKEN in secrets."""
    contract_path = _NODES_ROOT / node_name / "contract.yaml"
    assert contract_path.exists(), (
        f"Contract not found at {contract_path}. "
        "Node must exist and declare a contract.yaml."
    )
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{contract_path} must be a YAML mapping"
    secrets = raw.get("secrets")
    assert isinstance(secrets, dict), (
        f"{contract_path} missing 'secrets' block — OMN-12856 requires it."
    )
    assert "GITHUB_TOKEN" in secrets, (
        f"{contract_path} 'secrets' block does not declare GITHUB_TOKEN."
    )
    # contract_secret_ref must succeed (end-to-end)
    ref = contract_secret_ref(contract_path, "GITHUB_TOKEN")
    assert ref == "GITHUB_TOKEN"
