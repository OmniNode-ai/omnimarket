# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25564: the forwarder resolves its workspace roots from the host's declaration and names an unresolved one.

The lane declaration the forwarder dials is found from ``OMNIBASE_PATH`` and ``ONEX_WORKSPACE_CONFIG_ROOT``. On a host
where neither was exported by hand, the forward ended ``DelegateLaneSelectionError`` and ``onex run-node`` reported
``not_delivered`` with nothing saying why.

* AC1: with both unset, each root comes from the unit environment files the host installer rendered under the host
  config directory, and reaches the lane resolver (and, run in the process itself, the process environment).
* AC2: with no declaration, the forwarder's refusal names each root and where it looked.

Each case fails on the merge base, where the roots were read from the environment only.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omnimarket.nodes.node_runtime_bus_forward_effect import (
    adapter_core_forwarder as adapter,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.handlers.handler_workspace_roots_resolve import (
    HandlerWorkspaceRootsResolve,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.models.model_workspace_roots import (
    ModelWorkspaceRootsRequest,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.overlay import (
    ModelRuntimeBusForwardOverlay,
)

pytestmark = pytest.mark.unit

ROOT = "OMNIBASE_PATH"
CONFIG_ROOT = "ONEX_WORKSPACE_CONFIG_ROOT"
EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_runtime_bus_forward_effect/overlay.example.yaml"
)
TOPIC = "example.evt.omnimarket.reading-taken.v1"


class _Publisher:
    def __init__(self) -> None:
        self.topics: list[str] = []

    def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: dict[str, str]
    ) -> None:
        self.topics.append(topic)

    def flush(self, timeout_s: float) -> int:
        return 0

    def failure_detail(self) -> str | None:
        return None


def _overlay() -> ModelRuntimeBusForwardOverlay:
    return ModelRuntimeBusForwardOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


def _recorded() -> object:
    return SimpleNamespace(topic=TOPIC, key_b64=None, value_b64="b25l", headers={})


def _declare(config_dir: Path, name: str, lines: list[str]) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _forwarder(
    environ: dict[str, str] | None = None,
) -> adapter.CoreRuntimeBusForwarder:
    return adapter.CoreRuntimeBusForwarder(
        overlay=_overlay(),
        publisher_for_broker=lambda _b: _Publisher(),
        environ=environ,
    )


def test_ac1_both_roots_unset_resolve_from_the_host_units_and_reach_the_lane_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, private = tmp_path / "workspace", tmp_path / "private-config"
    config_dir = tmp_path / "cfg" / "onex"
    _declare(
        config_dir,
        "serve-a.env",
        [f"{ROOT}={workspace}", f"{CONFIG_ROOT}={workspace}/../private-config"],
    )
    _declare(
        config_dir,
        "serve-b.env",
        [f'export {ROOT}="{workspace}"', f"{CONFIG_ROOT}={private}"],
    )
    seen: list[dict[str, str]] = []
    monkeypatch.setattr(
        adapter, "broker_for_lane", lambda lane, env: seen.append(dict(env)) or lane
    )

    receipt = _forwarder({"XDG_CONFIG_HOME": str(tmp_path / "cfg")}).forward(
        [_recorded()]
    )

    assert receipt == {"forwarded": 1, "failed": 0, "detail": None}
    assert seen[0][ROOT] == str(workspace)
    assert seen[0][CONFIG_ROOT] == str(private)


def test_ac1_run_in_the_process_itself_the_roots_reach_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    for name in (ROOT, "OMNI_HOME", CONFIG_ROOT):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    config_dir = tmp_path / "cfg" / "onex"
    _declare(
        config_dir,
        "serve.env",
        [f"{ROOT}=/declared/ws", f"{CONFIG_ROOT}=/declared/cfg"],
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(adapter, "broker_for_lane", lambda lane, _env: lane)

    _forwarder().forward([_recorded()])

    assert (os.environ[ROOT], os.environ[CONFIG_ROOT]) == (
        "/declared/ws",
        "/declared/cfg",
    )


def test_ac2_no_declaration_refuses_naming_each_root_and_where_it_looked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "cfg" / "onex"
    config_dir.mkdir(parents=True)

    def refuse(_lane: str, _env: object) -> None:
        raise OSError("cannot locate the lane declaration: no workspace root is set")

    monkeypatch.setattr(adapter, "broker_for_lane", refuse)

    with pytest.raises(RuntimeError) as raised:
        _forwarder({"XDG_CONFIG_HOME": str(tmp_path / "cfg")}).forward([_recorded()])

    message = str(raised.value)
    assert "no workspace root is set" in message
    for name in (ROOT, CONFIG_ROOT):
        assert f"{name}: looked at" in message
    assert str(config_dir / "*.env") in message


def test_an_environment_root_is_never_replaced_by_the_host_declaration(
    tmp_path: Path,
) -> None:
    _declare(
        tmp_path / "onex", "serve.env", [f"{ROOT}=/declared", f"{CONFIG_ROOT}=/cfg"]
    )

    roots = HandlerWorkspaceRootsResolve().handle(
        ModelWorkspaceRootsRequest(
            environment={"XDG_CONFIG_HOME": str(tmp_path), ROOT: "/exported"}
        )
    )

    assert dict(roots.declared) == {CONFIG_ROOT: "/cfg"}
    assert roots.looked == ()


def test_omni_home_counts_as_a_declared_workspace_root(tmp_path: Path) -> None:
    _declare(tmp_path / "onex", "serve.env", [f"{ROOT}=/declared"])

    roots = HandlerWorkspaceRootsResolve().handle(
        ModelWorkspaceRootsRequest(
            environment={"XDG_CONFIG_HOME": str(tmp_path), "OMNI_HOME": "/registry"}
        )
    )

    assert ROOT not in roots.declared


def test_units_that_disagree_leave_the_root_unresolved_naming_each_file(
    tmp_path: Path,
) -> None:
    _declare(tmp_path / "onex", "a.env", [f"{ROOT}=/one"])
    _declare(tmp_path / "onex", "b.env", [f"{ROOT}=/two"])

    roots = HandlerWorkspaceRootsResolve().handle(
        ModelWorkspaceRootsRequest(environment={"XDG_CONFIG_HOME": str(tmp_path)})
    )

    described = roots.describe_unresolved() or ""
    assert ROOT not in roots.declared
    assert "a.env declare /one" in described
    assert "b.env declare /two" in described


def test_an_unexpanded_value_is_not_taken_as_a_path(tmp_path: Path) -> None:
    _declare(tmp_path / "onex", "a.env", [f"{ROOT}=$HOME/x"])

    roots = HandlerWorkspaceRootsResolve().handle(
        ModelWorkspaceRootsRequest(environment={"XDG_CONFIG_HOME": str(tmp_path)})
    )

    assert ROOT not in roots.declared
    assert "not an absolute literal path" in (roots.describe_unresolved() or "")
