# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compose package-declared setup capabilities before constructing any effects."""

import os
import stat
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path

import yaml
from omnibase_core.cli.cli_user_config import parse_user_config_text, user_config_path
from omnibase_core.types.type_serializable_value import SerializedDict
from pydantic import BaseModel

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plugin import (
    ModelSetupPlugin,
    ModelSetupPluginPreferences,
    ModelSetupPluginResolution,
)
from omnimarket.nodes.node_onex_setup_effect.protocols.protocol_setup_step import (
    ProtocolSetupStep,
)


class SetupPluginConfigurationError(ValueError):
    """Invalid user configuration without reproducing potentially secret input."""


class HandlerSetupPlugins:
    """Installed manifests select existing ONEX handlers; the journal stays authoritative."""

    def __init__(
        self,
        *,
        plugins: tuple[ModelSetupPlugin, ...] | None = None,
        builtins: tuple[ModelSetupPlugin, ...] | None = None,
        onex_home: Path | None = None,
    ) -> None:
        self.onex_home = onex_home or user_config_path().parent
        self.packages: dict[str, str] = {}
        if plugins is not None:
            self._plugins = plugins
            return
        if builtins is None:
            source = Path(__file__).resolve().parents[1] / "setup_plugins.yaml"
            document = yaml.safe_load(source.read_text())
            builtins = tuple(
                ModelSetupPlugin.model_validate(row) for row in document["plugins"]
            )
        extensions = []
        for point in sorted(entry_points(group="onex.setup"), key=lambda p: p.name):
            manifest = ModelSetupPlugin.model_validate(point.load())
            extensions.append(manifest)
            if point.dist is not None:
                self.packages[manifest.id] = point.dist.name
        self._plugins = (*builtins, *extensions)

    def configuration(self) -> SerializedDict:
        """Read bounded canonical configuration without creating or changing it."""
        path = user_config_path(self.onex_home)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise ValueError("Invalid plugin configuration file")
            raw = source.read(1024 * 1024 + 1).decode()
        return dict(parse_user_config_text(raw, str(path)) or {})

    def preferences(self) -> ModelSetupPluginPreferences:
        try:
            return self._preferences()
        except Exception:
            raise SetupPluginConfigurationError(
                "Invalid plugin configuration; review the setup_plugins section of config.yaml"
            ) from None

    def _preferences(self) -> ModelSetupPluginPreferences:
        preferences = ModelSetupPluginPreferences.model_validate(
            self.configuration().get("setup_plugins", {})
        )
        for plugin in self._plugins:
            if plugin.configuration_schema is not None:
                module, name = plugin.configuration_schema.split(":")
                schema = getattr(import_module(module), name)
                if not isinstance(schema, type) or not issubclass(schema, BaseModel):
                    raise ValueError(
                        f"Invalid plugin configuration schema: {plugin.id}"
                    )
                schema.model_validate(preferences.configuration.get(plugin.id, {}))
        return preferences

    def is_enabled(self, plugin: ModelSetupPlugin) -> bool:
        return self.preferences().enabled.get(plugin.id, plugin.enabled_by_default)

    def manifests(self) -> tuple[ModelSetupPlugin, ...]:
        self._validate()
        self.preferences()
        return self._plugins

    def plugin(self, name: str) -> ModelSetupPlugin:
        by_id = self._validate()
        if name not in by_id:
            raise ValueError(f"Unknown setup plugin: {name}")
        return by_id[name]

    def default_components(self) -> tuple[str, ...]:
        return tuple(
            component
            for plugin in self.manifests()
            if plugin.recommended and self.is_enabled(plugin)
            for component in plugin.components
        )

    def components(self) -> frozenset[str]:
        self._validate()
        return frozenset(c for plugin in self._plugins for c in plugin.components)

    def consent_notices(self, selected: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            plugin.consent_notice
            for plugin in self.resolve(selected)
            if plugin.consent_notice is not None
        )

    def handle(self, plan: ModelSetupPlan) -> ModelSetupPluginResolution:
        return ModelSetupPluginResolution(plugins=self.resolve(plan.components))

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
                if any(step.stage == "setup" for step in plugin.steps) and any(
                    step.stage == "completion" for step in by_id[dependency].steps
                ):
                    raise ValueError(
                        f"Setup plugin {plugin.id} cannot depend on completion plugin {dependency}"
                    )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError(f"Cyclic setup dependency: {name}")
            if name in visited:
                return
            visiting.add(name)
            for dependency in by_id[name].requires:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in by_id:
            visit(name)
        return by_id

    def resolve(
        self,
        selected: tuple[str, ...],
        *,
        disabled: tuple[str, ...] = (),
    ) -> tuple[ModelSetupPlugin, ...]:
        by_id = self._validate()
        disabled = tuple(
            set(disabled) | {p.id for p in self._plugins if not self.is_enabled(p)}
        )
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
        completions: list[ProtocolSetupStep] = []
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
                if binding.stage == "completion":
                    completions.append(effect)
                else:
                    effects.append(effect)
        return (*effects, *completions)
