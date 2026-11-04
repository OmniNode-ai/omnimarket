# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Guest steps are ssh commands to the address `tart ip` returned.

The Tart Guest Agent does not answer on the stock image, so `tart exec` cannot
carry a guest step. Authentication uses the image's documented default login: the
password is printed by an askpass program on the Tart host, never held here.
"""

from __future__ import annotations

import shlex

from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    ModelWalkRequest,
)

# The VM is a throwaway clone, so its host key is neither pinned nor remembered.
_SSH_OPTIONS = (
    "StrictHostKeyChecking=no",
    "UserKnownHostsFile=/dev/null",
    "ConnectTimeout=15",
    "PreferredAuthentications=password,keyboard-interactive",
    "PubkeyAuthentication=no",
    "NumberOfPasswordPrompts=1",
    "LogLevel=ERROR",
)


def require_guest_login(request: ModelWalkRequest) -> None:
    """Fail before any VM exists when the guest login cannot be built."""
    if request.guest_user is None or request.guest_askpass_path is None:
        raise ValueError(
            "guest steps run over ssh: set guest_askpass_path and resolve the "
            "request from the contract before the walk"
        )


def bind_guest_ip(request: ModelWalkRequest, tart_ip_stdout: str) -> ModelWalkRequest:
    """Return the request carrying the address `tart ip` printed, validated as IPv4."""
    return ModelWalkRequest.model_validate(
        {**request.model_dump(), "guest_ip": tart_ip_stdout.strip()}
    )


def guest_ssh_argv(request: ModelWalkRequest, command: str) -> tuple[str, ...]:
    """The ssh argv, run on the Tart host, that issues `command` in the guest."""
    if (
        request.guest_user is None
        or request.guest_askpass_path is None
        or request.guest_ip is None
    ):
        raise ValueError(
            "bind the tart ip and resolve the guest login before building guest commands"
        )
    options = [part for option in _SSH_OPTIONS for part in ("-o", option)]
    return (
        "env",
        f"SSH_ASKPASS={request.guest_askpass_path}",
        "SSH_ASKPASS_REQUIRE=force",
        "ssh",
        *options,
        f"{request.guest_user}@{request.guest_ip}",
        f"bash -lc {shlex.quote(command)}",
    )
