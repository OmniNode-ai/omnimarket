# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for scripts/proof_of_life_seam_parallel.py."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_seam_parallel_executor.models.model_seam_task import (
    ModelSeamParallelInput,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "proof_of_life_seam_parallel.py"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "proof_of_life_seam_parallel", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["proof_of_life_seam_parallel"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def proof_module() -> Any:
    return _load_script_module()


class FakeBus:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.published: list[tuple[str, bytes | None, bytes, object | None]] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object | None = None,
    ) -> None:
        self.published.append((topic, key, value, headers))


@pytest.mark.unit
def test_script_has_no_handler_imports() -> None:
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.append(node.module)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)

    assert all(".handlers" not in imported for imported in imports)


@pytest.mark.unit
def test_command_topic_is_contract_derived(proof_module: Any) -> None:
    assert (
        proof_module._load_command_topic()
        == "onex.cmd.omnimarket.seam-parallel-execute.v1"
    )


@pytest.mark.unit
async def test_publish_proof_of_life_publishes_typed_command_payloads(
    proof_module: Any,
) -> None:
    fake_bus = FakeBus()

    receipts = await proof_module.publish_proof_of_life(
        case_name="all",
        bus_factory=lambda: fake_bus,
    )

    assert fake_bus.started is True
    assert fake_bus.closed is True
    assert len(fake_bus.published) == 2
    assert len(receipts) == 2

    for topic, key, value, headers in fake_bus.published:
        assert topic == "onex.cmd.omnimarket.seam-parallel-execute.v1"
        assert key is None
        assert headers is None
        command = ModelSeamParallelInput.model_validate_json(value)
        assert command.correlation_id in {
            receipt.correlation_id for receipt in receipts
        }
        assert command.tasks


@pytest.mark.unit
async def test_dry_run_does_not_start_bus(proof_module: Any) -> None:
    fake_bus = FakeBus()

    receipts = await proof_module.publish_proof_of_life(
        case_name="independent",
        dry_run=True,
        bus_factory=lambda: fake_bus,
    )

    assert fake_bus.started is False
    assert fake_bus.closed is False
    assert fake_bus.published == []
    assert len(receipts) == 1
    assert receipts[0].dry_run is True
