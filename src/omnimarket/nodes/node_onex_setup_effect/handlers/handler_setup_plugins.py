# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compose package-declared setup capabilities before constructing any effects."""

from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path

import yaml

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plugin import (
    ModelSetupPlugin,
)
from omnimarket.nodes.node_onex_setup_effect.protocols.protocol_setup_step import (
    ProtocolSetupStep,
)


class HandlerSetupPlugins:
    """Installed manifests select existing ONEX handlers; the journal stays authoritative."""

    def __init__(
        self,
        *,
        plugins: tuple[ModelSetupPlugin, ...] | None = None,
        builtins: tuple[ModelSetupPlugin, ...] | None = None,
    ) -> None:
        if plugins is not None:
            self._plugins = plugins
            return
        if builtins is None:
            source = Path(__file__).resolve().parents[1] / "setup_plugins.yaml"
            document = yaml.safe_load(source.read_text())
            builtins = tuple(
                ModelSetupPlugin.model_validate(row) for row in document["plugins"]
            )
        extensions = tuple(
            ModelSetupPlugin.model_validate(point.load())
            for point in sorted(entry_points(group="onex.setup"), key=lambda p: p.name)
        )
        self._plugins = (*builtins, *extensions)

    def components(self) -> frozenset[str]:
        self._validate()
        return frozenset(c for plugin in self._plugins for c in plugin.components)

    def _validate(self) -> dict[str, ModelSetupPlugin]:
        by_id: dict[str, ModelSetupPlugin] = {}
        components: set[str] = set()
        steps: set[str] = set()
        for plugin in self._plugins:
            if plugin.id in by_id:
                raise ValueError(f"Duplicate setup plugin: {plugin.id}")
            by_id[plugin.id] = plugin
            for component in plugin.components:
                if component in components:
                    raise ValueError(f"Duplicate setup component owner: {component}")
                components.add(component)
            for step in plugin.steps:
                if step.name in steps:
                    raise ValueError(f"Duplicate setup step: {step.name}")
                if set(step.context) & step.options.keys():
                    raise ValueError(f"Conflicting setup context: {step.name}")
                steps.add(step.name)
        for plugin in self._plugins:
            for dependency in plugin.requires:
                if dependency not in by_id:
                    raise ValueError(
                        f"Missing setup dependency: {plugin.id} requires {dependency}"
                    )
        return by_id

    def resolve(
        self,
        selected: tuple[str, ...],
        *,
        disabled: tuple[str, ...] = (),
    ) -> tuple[ModelSetupPlugin, ...]:
        by_id = self._validate()
        known = {c for plugin in self._plugins for c in plugin.components}
        unknown = set(selected) - known
        if unknown:
            raise ValueError(
                "Unavailable setup components: " + ", ".join(sorted(unknown))
            )
        if set(disabled) - by_id.keys():
            raise ValueError("Unknown disabled setup plugin")
        visiting: set[str] = set()
        visited: set[str] = set()
        ordered: list[ModelSetupPlugin] = []

        def visit(name: str) -> None:
            if name in disabled:
                raise ValueError(f"Required setup plugin is disabled: {name}")
            if name in visiting:
                raise ValueError(f"Cyclic setup dependency: {name}")
            if name in visited:
                return
            visiting.add(name)
            for dependency in by_id[name].requires:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
            ordered.append(by_id[name])

        for plugin in self._plugins:
            if plugin.automatic or set(plugin.components) & set(selected):
                visit(plugin.id)
        return tuple(ordered)

    def steps(
        self,
        selected: tuple[str, ...],
        *,
        context: dict[str, object],
        disabled: tuple[str, ...] = (),
    ) -> tuple[ProtocolSetupStep, ...]:
        # Validate the complete selected graph before importing any factory.
        plugins = self.resolve(selected, disabled=disabled)
        effects: list[ProtocolSetupStep] = []
        for plugin in plugins:
            for binding in plugin.steps:
                module, name = binding.factory.split(":")
                factory = getattr(import_module(module), name)
                arguments: dict[str, object] = dict(binding.options)
                arguments.update({key: context[key] for key in binding.context})
                effect = factory(**arguments)
                if (
                    not isinstance(effect, ProtocolSetupStep)
                    or effect.name != binding.name
                ):
                    raise ValueError(
                        f"Invalid setup effect for {plugin.id}: {binding.name}"
                    )
                effects.append(effect)
        return tuple(effects)
