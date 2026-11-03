# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of overlay-bound handler dependencies (OMN-21595).

A node contract lists the protocols its handlers need under ``handler_dependencies``.
The lane overlay binds each protocol to an implementation and its configuration;
``omnibase_core``'s ``ModelOverlayBindings`` builds them from that binding alone. A
handler module reads no environment variable and imports no concrete client: it asks
this module for the dependencies its contract declares.

Two implementations exist for each protocol, so one handler test runs against either
binding without a source difference:

* in-memory: the configuration carries the values (``memory_*``);
* durable: the configuration names the file or the host the values come from
  (``file_*`` and ``host_*``).

``host_process_environment`` is the one place that reads the host process
environment; the nodes read it only through the protocol this module binds.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from omnibase_core.models.nodes.handler_dependencies.model_built_dependencies import (
    ModelBuiltDependencies,
)
from omnibase_core.models.nodes.handler_dependencies.model_overlay_bindings import (
    ModelOverlayBindings,
)

from omnimarket.protocols.protocol_overlay_document import ProtocolOverlayDocument
from omnimarket.protocols.protocol_process_environment import (
    ProtocolProcessEnvironment,
)

PROCESS_ENVIRONMENT_PROTOCOL = (
    f"{ProtocolProcessEnvironment.__module__}.{ProtocolProcessEnvironment.__qualname__}"
)
OVERLAY_DOCUMENT_PROTOCOL = (
    f"{ProtocolOverlayDocument.__module__}.{ProtocolOverlayDocument.__qualname__}"
)


class DependencyUnboundError(ValueError):
    """A contract declares a dependency the overlay does not bind; nothing was built."""

    def __init__(self, built: ModelBuiltDependencies) -> None:
        self.built = built
        reason = built.refusal_reason.value if built.refusal_reason else "unknown"
        super().__init__(
            f"{reason}: unbound={list(built.unbound)} "
            f"unregistered={list(built.unregistered)}"
        )


class _StaticEnvironment:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def environment(self) -> dict[str, str]:
        return dict(self._values)


class _HostEnvironment:
    def environment(self) -> dict[str, str]:
        return dict(os.environ)


class _InlineDocuments:
    """Documents whose YAML text the binding carries."""

    def __init__(self, texts: Mapping[str, str]) -> None:
        self._texts = dict(texts)

    def read(self, name: str) -> dict[str, Any] | None:
        text = self._texts.get(name)
        return None if text is None else _mapping(yaml.safe_load(text), name)


class _FileDocuments:
    """Documents whose YAML file the binding names; the file is read on each ask."""

    def __init__(self, paths: Mapping[str, str]) -> None:
        self._paths = dict(paths)

    def read(self, name: str) -> dict[str, Any] | None:
        path = self._paths.get(name)
        return (
            None if not path else _mapping(yaml.safe_load(Path(path).read_text()), name)
        )


def _mapping(loaded: object, name: str) -> dict[str, Any]:
    if not isinstance(loaded, dict):
        raise ValueError(f"overlay document {name} must be a mapping")
    return loaded


def memory_process_environment(config: Mapping[str, str]) -> object:
    return _StaticEnvironment(config)


def host_process_environment(config: Mapping[str, str]) -> object:
    return _HostEnvironment()


def memory_overlay_documents(config: Mapping[str, str]) -> object:
    return _InlineDocuments(config)


def file_overlay_documents(config: Mapping[str, str]) -> object:
    return _FileDocuments(config)


FACTORIES: dict[str, Callable[[Mapping[str, str]], object]] = {
    "memory_process_environment": memory_process_environment,
    "host_process_environment": host_process_environment,
    "memory_overlay_documents": memory_overlay_documents,
    "file_overlay_documents": file_overlay_documents,
}

_installed: list[ModelOverlayBindings] = []


@cache
def declared_handler_dependencies(package: str) -> tuple[str, ...]:
    """The ``handler_dependencies`` the contract packaged with ``package`` declares."""
    contract = yaml.safe_load(
        resources.files(package).joinpath("contract.yaml").read_text()
    )
    return tuple(contract.get("handler_dependencies", ()))


def packaged_contract_value(package: str, *keys: str) -> Any:
    """A value the contract packaged with ``package`` declares, found by its key path."""
    value: Any = yaml.safe_load(
        resources.files(package).joinpath("contract.yaml").read_text()
    )
    for key in keys:
        value = value[key]
    return value


def bind_overlay(bindings: ModelOverlayBindings | None) -> None:
    """Install the lane overlay this process runs on, or remove it with ``None``."""
    _installed.clear()
    if bindings is not None:
        _installed.append(bindings)


def installed_overlay() -> ModelOverlayBindings | None:
    return _installed[0] if _installed else None


def load_overlay_bindings(path: Path) -> ModelOverlayBindings:
    """Read a lane overlay file: the YAML form of ``ModelOverlayBindings``."""
    return ModelOverlayBindings.model_validate(yaml.safe_load(path.read_text()))


def bind_overlay_file(path: str) -> Path:
    """Install the lane overlay file at ``path``; a malformed file is a ``ValueError``."""
    try:
        bind_overlay(load_overlay_bindings(Path(path)))
    except (yaml.YAMLError, OSError) as error:
        raise ValueError("lane overlay file is unreadable or not valid YAML") from error
    return Path(path)


def build_dependencies(
    declared: Sequence[str], overlay: ModelOverlayBindings | None = None
) -> ModelBuiltDependencies:
    """Build ``declared`` from the overlay; with none installed every protocol is unbound."""
    bindings = overlay if overlay is not None else installed_overlay()
    if bindings is None:
        from omnibase_core.enums.enum_dependency_binding_refusal_reason import (
            EnumDependencyBindingRefusalReason,
        )

        return ModelBuiltDependencies(
            refusal_reason=EnumDependencyBindingRefusalReason.DEPENDENCY_UNBOUND,
            unbound=tuple(dict.fromkeys(declared)),
        )
    return bindings.build(declared, FACTORIES)


def require_dependencies(
    declared: Sequence[str], overlay: ModelOverlayBindings | None = None
) -> dict[str, object]:
    """Return the built dependencies by protocol id, or raise before anything runs."""
    built = build_dependencies(declared, overlay)
    if built.refusal_reason is not None:
        raise DependencyUnboundError(built)
    return built.dependencies


def process_environment(
    dependencies: Mapping[str, object],
) -> ProtocolProcessEnvironment:
    environment = dependencies[PROCESS_ENVIRONMENT_PROTOCOL]
    if not isinstance(environment, ProtocolProcessEnvironment):
        raise TypeError("bound process environment does not satisfy its protocol")
    return environment


def overlay_documents(dependencies: Mapping[str, object]) -> ProtocolOverlayDocument:
    documents = dependencies[OVERLAY_DOCUMENT_PROTOCOL]
    if not isinstance(documents, ProtocolOverlayDocument):
        raise TypeError("bound overlay documents do not satisfy their protocol")
    return documents


def installed_overlay_document(name: str) -> dict[str, Any] | None:
    """One named document of the installed overlay, or ``None`` when none is installed."""
    bindings = installed_overlay()
    if bindings is None:
        return None
    built = bindings.build([OVERLAY_DOCUMENT_PROTOCOL], FACTORIES)
    if built.refusal_reason is not None:
        return None
    return overlay_documents(built.dependencies).read(name)
