# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registered route resolves to the plain typed handler and its request model."""

from importlib import import_module

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_branch_protection_audit import (
    HandlerBranchProtectionAudit,
)
from omnimarket.nodes.node_branch_protection_audit_effect.models import (
    ModelBranchProtectionAuditRequest,
)

pytestmark = pytest.mark.unit


def test_registered_route() -> None:
    route = _resolve_node_route("node_branch_protection_audit_effect")
    assert (
        route.command_topic
        == "onex.cmd.omnimarket.branch-protection-audit-requested.v1"
    )
    assert route.terminal_topic == "onex.evt.omnimarket.branch-protection-audited.v1"
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerBranchProtectionAudit
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelBranchProtectionAuditRequest
    )
