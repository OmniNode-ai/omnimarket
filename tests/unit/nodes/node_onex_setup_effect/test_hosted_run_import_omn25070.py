# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex setup` loads on every omnibase-core the package allows (OMN-25070).

omnibase_core#2335 removed ``run_node_in_process`` from
``omnibase_core.cli.cli_run_node`` in favour of
``ModelRunNodeHosted.run``. A customer install resolves the newest core, so the
``setup`` entry point failed to import there and ``onex --help`` exited 1, while
this repository's own lock still pins a core from before the change.

Failure modes each test catches: the module failing to import on a core without
``run_node_in_process`` (the customer case), the module failing on a core
without ``ModelRunNodeHosted`` (the locked case), and the hosted run being
called with arguments in a different order than both cores expect.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest

CLI = "omnimarket.nodes.node_onex_setup_effect.cli"
OLD_HOME = "omnibase_core.cli.cli_run_node"
NEW_HOME = "omnibase_core.models.event_bus.model_run_node_hosted"


def _fresh_cli(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    package = importlib.import_module(CLI.rsplit(".", 1)[0])
    monkeypatch.setattr(package, "cli", getattr(package, "cli", None), raising=False)
    monkeypatch.delitem(sys.modules, CLI, raising=False)
    return importlib.import_module(CLI)


def _recording_runner(calls: list[tuple[Any, ...]]) -> Any:
    def run(
        node_id: str, contract_path: Path, payload: dict[str, object], timeout: int
    ) -> dict[str, object]:
        calls.append((node_id, contract_path, payload, timeout))
        return {}

    return run


@pytest.mark.unit
def test_loads_on_a_core_without_run_node_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = importlib.import_module(OLD_HOME)
    monkeypatch.delattr(old, "run_node_in_process", raising=False)
    calls: list[tuple[Any, ...]] = []
    hosted = types.ModuleType(NEW_HOME)
    hosted.ModelRunNodeHosted = type(  # type: ignore[attr-defined]
        "ModelRunNodeHosted", (), {"run": staticmethod(_recording_runner(calls))}
    )
    monkeypatch.setitem(sys.modules, NEW_HOME, hosted)

    cli = _fresh_cli(monkeypatch)

    cli._run_hosted_node("node_x", Path("contract.yaml"), {"a": 1}, 5)
    assert calls == [("node_x", Path("contract.yaml"), {"a": 1}, 5)]


@pytest.mark.unit
def test_loads_on_a_core_without_model_run_node_hosted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    old = importlib.import_module(OLD_HOME)
    monkeypatch.setattr(
        old, "run_node_in_process", _recording_runner(calls), raising=False
    )
    monkeypatch.setitem(sys.modules, NEW_HOME, None)

    cli = _fresh_cli(monkeypatch)

    cli._run_hosted_node("node_y", Path("c.yaml"), {}, 7)
    assert calls == [("node_y", Path("c.yaml"), {}, 7)]


@pytest.mark.unit
def test_a_broken_import_inside_the_new_home_is_raised_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = "omnibase_core.models.event_bus.model_runtime_bus_reading_missing"
    real_import = importlib.import_module

    def import_module(name: str, package: str | None = None) -> types.ModuleType:
        if name == NEW_HOME:
            raise ModuleNotFoundError(f"No module named {broken!r}", name=broken)
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", import_module)

    with pytest.raises(ModuleNotFoundError) as raised:
        _fresh_cli(monkeypatch)
    assert raised.value.name == broken
