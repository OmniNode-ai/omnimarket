# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public composition seam for the existing gateway settings owner."""

import os
import stat
from pathlib import Path
from typing import Literal

from omnibase_core.cli.cli_user_config import user_config_path
from omnibase_core.enums.enum_hook_bit import EnumHookBit, hook_enabled

from omnimarket.models.delegation.model_claude_gateway_hook import (
    ModelClaudeGatewayHookResult,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.handlers.gateway_probe import (
    probe_gateway,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.handlers.handler_claude_gateway_settings import (
    HandlerClaudeGatewaySettings,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.handlers.handler_gateway_hook_install import (
    HandlerGatewayHookInstall,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.models.model_claude_gateway_settings import (
    ModelClaudeGatewaySettingsRequest,
)
from omnimarket.nodes.node_claude_code_binding_install_effect.models.model_gateway_hook_install import (
    ModelGatewayHookInstallRequest,
    ModelGatewayHookInstallResult,
)


def manage_gateway_hooks(
    action: Literal["install", "status", "uninstall"],
    *,
    claude_home: Path | None = None,
) -> ModelGatewayHookInstallResult:
    """Use the existing settings owner from setup and other public compositions."""
    return HandlerGatewayHookInstall(claude_home=claude_home).handle(
        ModelGatewayHookInstallRequest(action=action)
    )


def gateway_hook_enabled() -> bool:
    """Read the same effective enforcer switch for setup and hook execution."""
    bit = EnumHookBit.POST_SKILL_DELEGATION_ENFORCER
    if "ONEX_HOOKS_MASK" in os.environ:
        return hook_enabled(bit)
    path = Path(
        os.environ.get("OMNIBASE_ENV_FILE", str(Path.home() / ".omnibase" / ".env"))
    )
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return hook_enabled(bit)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError("Unsafe hook mask source")
        raw = source.read(1024 * 1024 + 1).decode()
    for line in raw.splitlines():
        if line.strip().startswith("ONEX_HOOKS_MASK="):
            try:
                mask = int(line.strip().split("=", 1)[1], 0)
            except ValueError:
                return hook_enabled(bit)
            return hook_enabled(bit, mask=mask)
    return hook_enabled(bit)


def check_gateway_hook_binding(cwd: Path) -> ModelClaudeGatewayHookResult:
    handler = HandlerClaudeGatewaySettings(
        onex_home=user_config_path().parent,
        claude_home=Path(
            os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
        ),
        working_directory=cwd,
        probe=probe_gateway,
    )
    try:
        if not handler.configured():
            return ModelClaudeGatewayHookResult(decision="allow")
        result = handler.handle(
            ModelClaudeGatewaySettingsRequest(
                action="status", environment=dict(os.environ)
            )
        )
        if result.status == "success" and result.enabled:
            return ModelClaudeGatewayHookResult(decision="allow")
    except (OSError, ValueError):
        pass
    return ModelClaudeGatewayHookResult(
        decision="block",
        reason="ONEX gateway binding is unavailable or conflicts with Claude settings. Run onex claude status; restore the binding before retrying. No direct-provider fallback was used.",
    )
