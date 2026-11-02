# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane overlays for tests of handlers that take their dependencies from the overlay (OMN-21595)."""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping

import pytest
from omnibase_core.models.nodes.handler_dependencies.model_dependency_binding import (
    ModelDependencyBinding,
)
from omnibase_core.models.nodes.handler_dependencies.model_overlay_bindings import (
    ModelOverlayBindings,
)

from omnimarket.handlers.container.container_overlay_dependencies import (
    OVERLAY_DOCUMENT_PROTOCOL,
    PROCESS_ENVIRONMENT_PROTOCOL,
    bind_overlay,
)


def _overlay(
    environment: ModelDependencyBinding,
    documents: ModelDependencyBinding,
    *,
    lazy_start_budget_seconds: float = 30.0,
    reaper_flag_to_delete_days: float | None = None,
) -> ModelOverlayBindings:
    return ModelOverlayBindings(
        dependencies=(environment, documents),
        announcer_bus=ModelDependencyBinding(
            protocol="announcer_bus", implementation="unbuilt_announcer_bus"
        ),
        catalog_store=ModelDependencyBinding(
            protocol="catalog_store", implementation="unbuilt_catalog_store"
        ),
        lazy_start_budget_seconds=lazy_start_budget_seconds,
        reaper_flag_to_delete_days=reaper_flag_to_delete_days,
    )


def in_memory_overlay(
    *,
    environment: Mapping[str, str] | None = None,
    documents: Mapping[str, str] | None = None,
    lazy_start_budget_seconds: float = 30.0,
    reaper_flag_to_delete_days: float | None = None,
) -> ModelOverlayBindings:
    """An overlay whose bindings carry their values: environment mapping, YAML texts."""
    return _overlay(
        ModelDependencyBinding(
            protocol=PROCESS_ENVIRONMENT_PROTOCOL,
            implementation="memory_process_environment",
            config=dict(environment or {}),
        ),
        ModelDependencyBinding(
            protocol=OVERLAY_DOCUMENT_PROTOCOL,
            implementation="memory_overlay_documents",
            config=dict(documents or {}),
        ),
        lazy_start_budget_seconds=lazy_start_budget_seconds,
        reaper_flag_to_delete_days=reaper_flag_to_delete_days,
    )


def durable_overlay(
    *,
    document_paths: Mapping[str, str] | None = None,
    lazy_start_budget_seconds: float = 30.0,
    reaper_flag_to_delete_days: float | None = None,
) -> ModelOverlayBindings:
    """An overlay whose bindings name where the values live: the host, YAML files."""
    return _overlay(
        ModelDependencyBinding(
            protocol=PROCESS_ENVIRONMENT_PROTOCOL,
            implementation="host_process_environment",
        ),
        ModelDependencyBinding(
            protocol=OVERLAY_DOCUMENT_PROTOCOL,
            implementation="file_overlay_documents",
            config=dict(document_paths or {}),
        ),
        lazy_start_budget_seconds=lazy_start_budget_seconds,
        reaper_flag_to_delete_days=reaper_flag_to_delete_days,
    )


def host_overlay_fixture() -> Callable[[], Generator[None, None, None]]:
    """An autouse fixture body that binds the host-environment overlay for one test."""

    def _fixture() -> Generator[None, None, None]:
        bind_overlay(durable_overlay())
        yield
        bind_overlay(None)

    return _fixture


@pytest.fixture
def unbound_overlay() -> Generator[None, None, None]:
    bind_overlay(None)
    yield
    bind_overlay(None)
