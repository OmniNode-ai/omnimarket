# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostShellTransport: the ssh and local adapter of ProtocolHostShell (OMN-19130).

ssh runs in batch mode with the overlay's options, so it never prompts; the
script goes down ``ssh <target> [sudo -n] sh -s`` on stdin. The local transport
runs ``[sudo -n] sh -s`` on the launching host. ssh's own exit 255, a timeout,
or a shell that cannot start reads as ``transport_failed``: the script never ran.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

from omnimarket.models.host_bringup.model_host_shell import (
    EnumHostTransportKind,
    ModelHostShellResult,
    ModelHostTransport,
)

SSH_TRANSPORT_EXIT = 255
_STDERR_LIMIT = 4000

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class HandlerHostShellTransport:
    def __init__(
        self,
        *,
        ssh_options: Sequence[str],
        connect_timeout_s: int,
        runner: Runner = subprocess.run,
    ) -> None:
        self._ssh_options = tuple(ssh_options)
        self._connect_timeout_s = connect_timeout_s
        self._runner = runner

    def argv(self, transport: ModelHostTransport, *, privileged: bool) -> list[str]:
        shell = ["sudo", "-n", "sh", "-s"] if privileged else ["sh", "-s"]
        if transport.kind is EnumHostTransportKind.LOCAL:
            return shell
        options: list[str] = []
        for option in (
            "BatchMode=yes",
            f"ConnectTimeout={self._connect_timeout_s}",
            *self._ssh_options,
        ):
            options += ["-o", option]
        assert transport.target is not None
        return ["ssh", *options, transport.target, *shell]

    def run(
        self,
        transport: ModelHostTransport,
        script: str,
        *,
        privileged: bool,
        timeout_s: int,
    ) -> ModelHostShellResult:
        argv = self.argv(transport, privileged=privileged)
        try:
            done = self._runner(
                argv,
                input=script,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return ModelHostShellResult(
                exit_code=-1,
                stderr=f"no answer within {timeout_s}s",
                transport_failed=True,
            )
        except OSError as exc:
            return ModelHostShellResult(
                exit_code=-1, stderr=f"{argv[0]}: {exc}", transport_failed=True
            )
        failed = (
            transport.kind is EnumHostTransportKind.SSH
            and done.returncode == SSH_TRANSPORT_EXIT
        )
        return ModelHostShellResult(
            exit_code=done.returncode,
            stdout=done.stdout,
            stderr=done.stderr[-_STDERR_LIMIT:],
            transport_failed=failed,
        )


__all__ = ["SSH_TRANSPORT_EXIT", "HandlerHostShellTransport"]
