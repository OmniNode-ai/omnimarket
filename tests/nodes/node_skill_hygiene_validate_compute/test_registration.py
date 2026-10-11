# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registered route resolves to the plain typed handler and its request model."""

from importlib import import_module

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.nodes.node_skill_hygiene_validate_compute.handlers.handler_skill_hygiene_validate import (
    HandlerSkillHygieneValidate,
)
from omnimarket.nodes.node_skill_hygiene_validate_compute.models import (
    ModelSkillHygieneValidateRequest,
)

pytestmark = pytest.mark.unit


def test_registered_route() -> None:
    route = _resolve_node_route("node_skill_hygiene_validate_compute")
    assert (
        route.command_topic == "onex.cmd.omnimarket.skill-hygiene-validate-requested.v1"
    )
    assert route.terminal_topic == "onex.evt.omnimarket.skill-hygiene-validated.v1"
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerSkillHygieneValidate
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelSkillHygieneValidateRequest
    )
