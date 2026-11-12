# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failure-first capability composition controls, without model inference.

Unknown installed capabilities must work without editing the engine. Missing,
disabled, cyclic and duplicate declarations must fail before setup writes state
or constructs an effect. Unselected plugins must never run.
"""

from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_plugins import (
    HandlerSetupPlugins,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_workflow import (
    HandlerSetupWorkflow,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plugin import (
    ModelSetupPlugin,
)

pytestmark = pytest.mark.unit


def manifest(
    name: str, *, requires: tuple[str, ...] = (), automatic: bool = False
) -> ModelSetupPlugin:
    return ModelSetupPlugin.model_validate(
        {
            "id": name,
            "api_version": 1,
            "version": "1.0.0",
            "components": () if automatic else (name,),
            "requires": requires,
            "automatic": automatic,
            "steps": [{"name": name, "factory": "unloaded.module:Factory"}],
        }
    )


def test_dependency_order_selects_only_requested_plugins() -> None:
    registry = HandlerSetupPlugins(
        plugins=(
            manifest("unused"),
            manifest("feature", requires=("base",)),
            manifest("base", automatic=True),
        )
    )
    assert [p.id for p in registry.resolve(("feature",), disabled=("unused",))] == [
        "base",
        "feature",
    ]


@pytest.mark.parametrize(
    "case",
    ["duplicate", "missing", "cycle", "step", "component", "disabled", "unknown"],
)
def test_invalid_graph_is_rejected_before_loading_effect_factories(case: str) -> None:
    a, b = manifest("a"), manifest("b")
    plugins = (a, b)
    disabled: tuple[str, ...] = ()
    selected = ("a",)
    if case == "duplicate":
        plugins = (a, a)
    if case == "missing":
        plugins = (manifest("a", requires=("absent",)),)
    if case == "cycle":
        plugins = (manifest("a", requires=("b",)), manifest("b", requires=("a",)))
    if case == "step":
        plugins = (a, b.model_copy(update={"steps": a.steps}))
    if case == "component":
        plugins = (a, b.model_copy(update={"components": a.components}))
    if case == "disabled":
        disabled = ("a",)
    if case == "unknown":
        selected = ("unknown",)
    with pytest.raises(ValueError, match="setup"):
        HandlerSetupPlugins(plugins=plugins).resolve(selected, disabled=disabled)


def test_unsupported_manifest_version_fails_closed() -> None:
    with pytest.raises(ValidationError):
        ModelSetupPlugin.model_validate(manifest("a").model_dump() | {"api_version": 2})


def test_external_entrypoint_capability_runs_through_existing_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers import handler_setup_plugins

    module = tmp_path / "customer_extension.py"
    module.write_text("""
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import ModelSetupObservation
PLUGIN = {"id": "customer", "api_version": 1, "version": "1.0.0", "components": ["customer"], "requires": [], "steps": [{"name": "customer", "factory": "customer_extension:Effect", "context": ["onex_home"]}]}
class Effect:
    name = "customer"
    def __init__(self, *, onex_home): self.marker = onex_home / "customer-marker"
    def observe(self, plan): return ModelSetupObservation(step=self.name, status="success" if self.marker.exists() else "unavailable")
    def apply(self, plan): self.marker.write_text("enabled")
""")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        handler_setup_plugins,
        "entry_points",
        lambda **_: (
            EntryPoint(
                name="customer", value="customer_extension:PLUGIN", group="onex.setup"
            ),
        ),
    )
    registry = HandlerSetupPlugins(builtins=())
    home = tmp_path / "config"
    engine = HandlerSetupWorkflow(onex_home=home, plugins=registry)
    plan = ModelSetupPlan(workspace=tmp_path / "work", components=("customer",))
    first = engine.handle(plan)
    assert first.observations[-1].status == "success"
    state = (home / "setup.json").read_bytes()
    before = (home / "customer-marker").stat().st_mtime_ns
    second = engine.handle(plan)
    assert all(not row.changed for row in second.observations)
    assert (home / "setup.json").read_bytes() == state
    assert (home / "customer-marker").stat().st_mtime_ns == before
