# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""LocalShellFocusedRunSubprocess -- the focused-run client for a process that
runs ON the lab docker host (OMN-19458).

The ssh-backed client (OMN-19359) runs on the launching machine and reaches
the lab host over ssh. The bus-hosted node (``node_focused_test_run_effect``)
runs on the lab host itself, so the same scripts run under a local
``/bin/sh -c`` instead: no ssh, no credential to another machine, and no
docker socket inside any runtime container (operator ruling 2026-09-25,
OMN-19458). Every other step -- admission, orphan sweep, mirror and worktree,
environment image, overlay, container argv validation, teardown -- is the
parent's, unchanged.

``ONEX_DTL_HOST`` is not read: the host is the machine this process runs on.
``ONEX_DTL_TASK_ROOT``, ``ONEX_DTL_TEST_IMAGE`` and ``ONEX_DTL_ENV_DOCKERFILE``
are still required, exactly as for the parent.
"""

from __future__ import annotations

import socket
import subprocess

from omnimarket.nodes.node_push_validation_effect.protocols.ephemeral_container_focused_run_subprocess import (
    EphemeralContainerFocusedRunSubprocess,
    _SshWallClockExceededError,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_focused_test_run_client import (
    FocusedTestRunInfraError,
)

__all__ = ["LocalShellFocusedRunSubprocess"]


class LocalShellFocusedRunSubprocess(EphemeralContainerFocusedRunSubprocess):
    """Focused run subprocess that executes commands locally via /bin/sh."""

    def _ssh(
        self,
        script: str,
        *,
        stdin: bytes | None = None,
        timeout: int = 120,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run the script locally using /bin/sh -c.

        Args:
            script: The shell script to execute.
            stdin: Optional bytes to pass to the process's stdin.
            timeout: Timeout in seconds for the process.
            check: If True, raise an error on non-zero exit codes.

        Returns:
            The completed process result.

        Raises:
            _SshWallClockExceededError: If the process times out.
            FocusedTestRunInfraError: If check is True and the process fails.
        """
        argv = ["/bin/sh", "-c", script]
        try:
            result = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise _SshWallClockExceededError(
                f"Local shell command timed out after {timeout}s"
            ) from exc

        if check and result.returncode != 0:
            stderr_tail = result.stderr.decode(errors="replace")[-400:]
            raise FocusedTestRunInfraError(
                f"Local shell command failed with exit code {result.returncode}: {stderr_tail}"
            )

        return result

    def host_identity(self) -> str:
        """Return the local host identity.

        Returns:
            A string identifying the local host.
        """
        return "local:" + socket.gethostname()
