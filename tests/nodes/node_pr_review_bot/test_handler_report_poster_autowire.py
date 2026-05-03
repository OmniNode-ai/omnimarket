# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime auto-wiring regression coverage for HandlerReportPoster."""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_handler_resolution_outcome import (
    EnumHandlerResolutionOutcome,
)
from omnibase_core.models.resolver.model_handler_resolver_context import (
    ModelHandlerResolverContext,
)
from omnibase_core.services.service_handler_resolver import ServiceHandlerResolver

from omnimarket.nodes.node_pr_review_bot.handlers.handler_emergency_bypass_parser import (
    HandlerEmergencyBypassParser,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_report_poster import (
    HandlerReportPoster,
)


@pytest.mark.unit
def test_handler_report_poster_resolves_via_zero_arg_auto_wiring() -> None:
    """HandlerReportPoster must not require github_bridge during runtime boot."""
    context = ModelHandlerResolverContext(
        handler_cls=HandlerReportPoster,
        handler_module=HandlerReportPoster.__module__,
        handler_name=HandlerReportPoster.__name__,
        contract_name="pr_review_bot",
        node_name="pr_review_bot",
    )

    resolution = ServiceHandlerResolver().resolve(context)

    assert resolution.outcome == EnumHandlerResolutionOutcome.RESOLVED_VIA_ZERO_ARG
    assert isinstance(resolution.handler_instance, HandlerReportPoster)


@pytest.mark.unit
def test_handler_emergency_bypass_parser_resolves_via_zero_arg_lockdown() -> None:
    """Runtime boot must wire bypass parsing without granting unaudited bypasses."""
    context = ModelHandlerResolverContext(
        handler_cls=HandlerEmergencyBypassParser,
        handler_module=HandlerEmergencyBypassParser.__module__,
        handler_name=HandlerEmergencyBypassParser.__name__,
        contract_name="pr_review_bot",
        node_name="pr_review_bot",
    )

    resolution = ServiceHandlerResolver().resolve(context)

    assert resolution.outcome == EnumHandlerResolutionOutcome.RESOLVED_VIA_ZERO_ARG
    assert isinstance(resolution.handler_instance, HandlerEmergencyBypassParser)
