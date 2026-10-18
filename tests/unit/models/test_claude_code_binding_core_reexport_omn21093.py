# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude Code binding install wire family is core's (OMN-21093).

The models moved to omnibase_core so shared wire types sit below the node
boundary; every former omnimarket import path must re-export the identical
core class, never a fork. These identity pins fail on the omnimarket revision
before the move (the core package does not exist there yet).
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_binding_kind import EnumBindingKind as CoreBindingKind
from omnibase_core.enums.enum_client_max_mode import (
    EnumClientMaxMode as CoreClientMaxMode,
)
from omnibase_core.enums.enum_client_verdict_action import (
    EnumClientVerdictAction as CoreVerdictAction,
)
from omnibase_core.models.claude_code_binding import (
    ModelBindingInstallRequest as CoreInstallRequest,
)
from omnibase_core.models.claude_code_binding import (
    ModelBindingInstallResult as CoreInstallResult,
)
from omnibase_core.models.claude_code_binding import (
    ModelClientArgument as CoreClientArgument,
)
from omnibase_core.models.claude_code_binding import (
    ModelClientBinding as CoreClientBinding,
)

from omnimarket.models.claude_code_binding import (
    EnumBindingKind,
    EnumClientMaxMode,
    EnumClientVerdictAction,
    ModelClientArgument,
    ModelClientBinding,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.models import (
    ModelBindingInstallRequest,
    ModelBindingInstallResult,
)

pytestmark = pytest.mark.unit


def test_enums_reexport_core_identities() -> None:
    assert EnumBindingKind is CoreBindingKind
    assert EnumClientMaxMode is CoreClientMaxMode
    assert EnumClientVerdictAction is CoreVerdictAction


def test_models_reexport_core_identities() -> None:
    assert ModelClientArgument is CoreClientArgument
    assert ModelClientBinding is CoreClientBinding


def test_node_install_models_reexport_core_identities() -> None:
    assert ModelBindingInstallRequest is CoreInstallRequest
    assert ModelBindingInstallResult is CoreInstallResult


def test_node_request_still_validates_the_same_shapes() -> None:
    request = ModelBindingInstallRequest(
        bindings=(
            ModelClientBinding(
                name="session_delegate",
                hook_event="SessionStart",
                command_topic="onex.cmd.demo.session_delegate.v1",
                command_type="ModelSessionDelegateCommand",
                verdict_compute="compute_session_delegate_verdict",
                deadline_ms=30000,
            ),
        ),
        max_mode=EnumClientMaxMode.SELECTIVE,
        mods_dir="/opt/demo/mods",
        settings_path="/opt/demo/settings.json",
        supported_probe_rows=("row.demo",),
    )
    assert request.plugin_name == "onex-claude-code-bindings"
    assert request.bindings[0].kind is EnumBindingKind.REPLACING
