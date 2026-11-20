# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex setup`` loads on the installed omnibase_core and reaches the core's hosted run.

omnibase_core removed ``cli_run_node.run_node_in_process`` and moved the in-process hosting
to ``ModelRunNodeHosted.run`` (same four arguments, same returned dict). The setup CLI named
the removed function, so a virtual environment built with the newest core could not load the
``onex`` CLI at all, while this repository's own lock, which pins an older core, hid it. These
tests read the installed core, never a stand-in, so they fail on a core that lacks a name the
module imports.
"""

from __future__ import annotations

import ast
import importlib
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.event_bus.model_run_node_hosted import ModelRunNodeHosted

from omnimarket.nodes.node_onex_setup_effect import cli
from omnimarket.nodes.node_onex_setup_effect.models import ModelOnexSetupRequest

pytestmark = pytest.mark.unit

_CORE_RUN_MODULES = (
    "omnibase_core.cli.cli_run_node",
    "omnibase_core.models.event_bus.model_run_node_hosted",
)


def _names_imported_from(module_name: str) -> list[str]:
    source = Path(str(cli.__file__)).read_text(encoding="utf-8")
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == module_name:
            names.extend(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("module_name", _CORE_RUN_MODULES)
def test_every_name_the_setup_cli_imports_from_the_core_run_modules_exists(
    module_name: str,
) -> None:
    installed = importlib.import_module(module_name)
    missing = [
        name
        for name in _names_imported_from(module_name)
        if not hasattr(installed, name)
    ]
    assert missing == [], (
        f"{module_name} of the installed omnibase_core lacks {missing}"
    )


def test_the_setup_cli_reaches_the_hosted_run_with_the_contract_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    contract_path = Path(str(resources.files(cli.NODE_PACKAGE) / "contract.yaml"))
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    calls: list[tuple[Any, ...]] = []

    def _hosted_run(
        node_id: str, path: Path, payload: dict[str, object], timeout: int
    ) -> dict[str, object]:
        calls.append((node_id, path, payload, timeout))
        return {"workspace": None, "install_path": None, "steps": []}

    # Only the run is replaced: the import under test stays the real one.
    monkeypatch.setattr(ModelRunNodeHosted, "run", staticmethod(_hosted_run))
    request = ModelOnexSetupRequest(onex_home=tmp_path, unattended=True)

    result = cli._run_through_runtime(request)

    assert result.steps == ()
    assert len(calls) == 1
    node_id, path, payload, timeout = calls[0]
    assert node_id == cli.NODE_NAME
    assert path == contract_path
    assert timeout == int(contract["descriptor"]["timeout_ms"]) // 1000
    assert payload["unattended"] is True
    assert "key" not in payload
