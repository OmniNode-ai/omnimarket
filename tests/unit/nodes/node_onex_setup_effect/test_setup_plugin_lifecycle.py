# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dependency cycles and recommended choices must be derived before effects."""

from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_plugins import (
    HandlerSetupPlugins,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plugin import (
    ModelSetupPlugin,
)

pytestmark = pytest.mark.unit


def plugin(name: str, **values: object) -> ModelSetupPlugin:
    return ModelSetupPlugin.model_validate(
        {
            "id": name,
            "api_version": 1,
            "version": "1.0.0",
            "components": [name],
            "steps": [{"name": name, "factory": "not_imported:Effect"}],
            **values,
        }
    )


def test_unselected_cycle_fails_before_any_effect() -> None:
    registry = HandlerSetupPlugins(
        plugins=(plugin("a"), plugin("b", requires=["c"]), plugin("c", requires=["b"]))
    )
    with pytest.raises(ValueError, match="Cyclic"):
        registry.resolve(("a",))


def test_recommended_components_obey_manifest_and_disabled_state(
    tmp_path: Path,
) -> None:
    registry = HandlerSetupPlugins(
        onex_home=tmp_path, plugins=(plugin("a", recommended=True), plugin("b"))
    )
    assert registry.default_components() == ("a",)
    (tmp_path / "config.yaml").write_text("setup_plugins: {enabled: {a: false}}\n")
    assert registry.default_components() == ()
    with pytest.raises(ValueError, match="disabled"):
        registry.resolve(("a",))
