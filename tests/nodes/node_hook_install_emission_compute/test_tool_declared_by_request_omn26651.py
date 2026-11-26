# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The captured tool is named by the request, never by a literal in the model (OMN-26651).

The hard-coded model guard refuses an engine label in source. The tool whose events a
projection reads is data the request declares, so the model accepts any non-empty name
and refuses an empty one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_hook_install_emission_compute.models import (
    ModelHookInstallDeclared,
    ModelHookInstallObservedEvent,
)

_T0 = datetime(2026, 11, 24, 17, 0, tzinfo=UTC)


def test_declared_tool_is_any_name_the_request_gives() -> None:
    install = ModelHookInstallDeclared(host="h053", tool="fixture-tool")
    assert install.tool == "fixture-tool"


def test_observed_actor_is_any_name_the_projection_gives() -> None:
    event = ModelHookInstallObservedEvent(
        host="h053", actor="fixture-tool", hook_event_name="Stop", emitted_at=_T0
    )
    assert event.actor == "fixture-tool"


def test_empty_tool_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelHookInstallDeclared(host="h053", tool="")
