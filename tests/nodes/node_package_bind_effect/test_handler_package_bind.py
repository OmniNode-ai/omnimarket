# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The package bind handler binds a node package without its ``__init__`` and refuses what it cannot bind (OMN-24321)."""

from __future__ import annotations

import importlib
import importlib.resources
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_package_bind_effect.handlers.handler_package_bind import (
    HandlerPackageBind,
)
from omnimarket.nodes.node_package_bind_effect.models.model_package_bind import (
    ModelPackageBindRequest,
)
from tests.nodes.node_package_bind_effect.conftest import DEMO, PACKAGE, write_tree

pytestmark = pytest.mark.unit


def _request(src: Path | str, node: str = DEMO) -> ModelPackageBindRequest:
    return ModelPackageBindRequest(correlation_id="c-1", src=str(src), node=node)


def test_failing_control_the_plain_import_runs_the_package_init(tmp_path: Path) -> None:
    """Without the bind, importing the demo package raises (fresh interpreter, the fake tree first on the path)."""
    root = write_tree(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            f"import sys; sys.path.insert(0, {str(root)!r}); import {PACKAGE}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "RuntimeError: package init ran" in proc.stderr


def test_the_bind_serves_the_handlers_and_the_contract_without_the_init(
    demo_tree: Path,
) -> None:
    result = HandlerPackageBind().handle(_request(demo_tree))
    assert (result.outcome, result.package, result.reason) == ("bound", PACKAGE, "")
    assert result.path == str(demo_tree / "omnimarket" / "nodes" / DEMO)
    handler = importlib.import_module(f"{PACKAGE}.handlers.handler_demo")
    assert handler.VALUE == 7
    text = (
        importlib.resources.files(PACKAGE)
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    assert text.strip() == f"name: {DEMO}"


def test_a_missing_node_directory_is_refused_and_binds_nothing(tmp_path: Path) -> None:
    result = HandlerPackageBind().handle(_request(tmp_path, "node_absent_compute"))
    assert result.outcome == "refused"
    assert result.reason.startswith("no-package-dir:")
    assert "node_absent_compute" in result.reason
    assert "omnimarket.nodes.node_absent_compute" not in sys.modules


@pytest.mark.parametrize("bad", ["../x", "Node-Demo", "node_a/b", "demo", ""])
def test_a_name_that_is_not_a_node_directory_is_refused(
    bad: str, tmp_path: Path
) -> None:
    result = HandlerPackageBind().handle(_request(tmp_path, bad))
    assert result.outcome == "refused"
    assert result.reason == f"bad-node-name:{bad}"
