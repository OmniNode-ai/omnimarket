# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
#
# Integration tests: topic-naming-lint producer allowlist in omnimarket CI. (OMN-8507)
#
# TDD: tests written before omnimarket hook wiring. Asserts the linter rejects
# unknown producer segments and accepts known ones when scanning fixture contracts.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


# Resolve omnibase_infra path:
# 1. OMNIBASE_INFRA_PATH env var (explicit override)
# 2. Sibling of this repo's root (worktree convention: both worktrees under same ticket dir)
# 3. $OMNI_HOME/omnibase_infra (canonical registry fallback)
def _find_lint_py() -> Path:
    if "OMNIBASE_INFRA_PATH" in os.environ:
        return (
            Path(os.environ["OMNIBASE_INFRA_PATH"])
            / "scripts/validation/lint_topic_names.py"
        )
    # Worktree layout: /.../<ticket>/omnimarket and /.../<ticket>/omnibase_infra
    worktree_sibling = (
        Path(__file__).parents[3]
        / "omnibase_infra/scripts/validation/lint_topic_names.py"
    )
    if worktree_sibling.exists():
        return worktree_sibling
    omni_home = os.environ.get("OMNI_HOME", "")
    if omni_home:
        return Path(omni_home) / "omnibase_infra/scripts/validation/lint_topic_names.py"
    return (
        Path(__file__).parents[3]
        / "../omnibase_infra/scripts/validation/lint_topic_names.py"
    )


_LINT_PY = _find_lint_py()


def _write_contract(tmp_path: Path, topics: list[str]) -> Path:
    contract_dir = tmp_path / "node_fixture"
    contract_dir.mkdir()
    contract: dict[str, object] = {
        "name": "fixture-node",
        "event_bus": {
            "publish_topics": topics,
        },
    }
    path = contract_dir / "contract.yaml"
    path.write_text(yaml.dump(contract), encoding="utf-8")
    return tmp_path


@pytest.mark.skipif(
    not _LINT_PY.exists(), reason="omnibase_infra not available at sibling path"
)
@pytest.mark.unit
def test_lint_rejects_unknown_producer_in_fixture_contract(tmp_path: Path) -> None:
    """Linter exits 1 when a fixture contract uses an unknown producer segment."""
    contracts_root = _write_contract(tmp_path, ["onex.evt.review-bot.foo.v1"])
    result = subprocess.run(
        [
            sys.executable,
            str(_LINT_PY),
            "--scan-contracts",
            str(contracts_root),
            "--no-baseline",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, (
        f"Expected exit 1, got {result.returncode}\nstderr: {result.stderr}"
    )
    assert "unknown producer" in result.stderr


@pytest.mark.skipif(
    not _LINT_PY.exists(), reason="omnibase_infra not available at sibling path"
)
@pytest.mark.unit
def test_lint_accepts_omnimarket_producer_in_fixture_contract(tmp_path: Path) -> None:
    """Linter exits 0 when a fixture contract uses the omnimarket producer segment."""
    contracts_root = _write_contract(
        tmp_path, ["onex.evt.omnimarket.review-bot-foo.v1"]
    )
    result = subprocess.run(
        [
            sys.executable,
            str(_LINT_PY),
            "--scan-contracts",
            str(contracts_root),
            "--no-baseline",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\nstderr: {result.stderr}"
    )


_NODE_OVERNIGHT_CONTRACTS = (
    Path(__file__).parents[2] / "src/omnimarket/nodes/node_overnight"
)
_NODE_OVERNIGHT_SRC = Path(__file__).parents[2] / "src/omnimarket/nodes/node_overnight"


@pytest.mark.skipif(
    not _LINT_PY.exists(), reason="omnibase_infra not available at sibling path"
)
@pytest.mark.unit
def test_node_overnight_contracts_pass_topic_naming_lint() -> None:
    """node_overnight contract.yaml topics must conform to the 5-segment spec (OMN-8507)."""
    result = subprocess.run(
        [
            sys.executable,
            str(_LINT_PY),
            "--scan-contracts",
            str(_NODE_OVERNIGHT_CONTRACTS),
            "--no-baseline",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"node_overnight contract topics violate naming spec:\n{result.stderr}"
    )


@pytest.mark.skipif(
    not _LINT_PY.exists(), reason="omnibase_infra not available at sibling path"
)
@pytest.mark.unit
def test_node_overnight_python_passes_topic_naming_lint() -> None:
    """node_overnight Python source must not contain 6-segment topic literals (OMN-8507)."""
    result = subprocess.run(
        [
            sys.executable,
            str(_LINT_PY),
            "--scan-python",
            str(_NODE_OVERNIGHT_SRC),
            "--no-baseline",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"node_overnight Python source contains violating topic literals:\n{result.stderr}"
    )
