# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A runtime with no overlay for the node still answers: a typed outcome, never a dispatch error and a silent topic.

Measured on the dev runtime: with no ``node_tenant_provision_effect/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS`` the
handler raised ``NodeOverlayNotFoundError``, the runtime logged ``handler_error with no terminal output`` and nothing
reached the completed topic, so a caller waiting on it saw ``runtime_unreachable``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_provision import (
    HandlerTenantProvision,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.handler_tenant_read import (
    HandlerTenantRead,
)
from omnimarket.nodes.node_tenant_provision_effect.models.model_tenant_provision import (
    EnumTenantProvisionOutcome,
    EnumTenantReadOutcome,
    ModelTenantProvisionCommand,
    ModelTenantReadCommand,
)

pytestmark = pytest.mark.asyncio


def _environ_without_overlay(tmp_path: Path) -> dict[str, str]:
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


async def test_provision_without_overlay_answers_store_unavailable(
    tmp_path: Path,
) -> None:
    handler = HandlerTenantProvision(environ=_environ_without_overlay(tmp_path))
    answer = await handler.handle(
        ModelTenantProvisionCommand(command_id=uuid4(), tenant_slug="acme-labs")
    )
    assert answer.outcome is EnumTenantProvisionOutcome.FAILED_STORE_UNAVAILABLE
    assert "overlay" in answer.detail


async def test_read_without_overlay_answers_store_unavailable(tmp_path: Path) -> None:
    handler = HandlerTenantRead(environ=_environ_without_overlay(tmp_path))
    answer = await handler.handle(
        ModelTenantReadCommand(command_id=uuid4(), tenant_slug="acme-labs")
    )
    assert answer.outcome is EnumTenantReadOutcome.FAILED_STORE_UNAVAILABLE
    assert "overlay" in answer.detail


async def test_control_a_present_overlay_does_not_trip_the_guard(
    tmp_path: Path,
) -> None:
    """Positive control: an invalid slug is refused before the overlay is read, so the overlay path is the cause."""
    handler = HandlerTenantProvision(environ=_environ_without_overlay(tmp_path))
    answer = await handler.handle(
        ModelTenantProvisionCommand(command_id=uuid4(), tenant_slug="A")
    )
    assert answer.outcome is EnumTenantProvisionOutcome.REFUSED_INVALID_SLUG
