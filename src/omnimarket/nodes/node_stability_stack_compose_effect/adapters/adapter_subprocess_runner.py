# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AdapterSubprocessRunner — real ``subprocess`` implementation of the command runner.

Thin subprocess wrapper for the compose effect node (OMN-23968): it runs the
argv list it is given with the environment and timeout it is given and returns
the typed output. No shell is ever involved (``shell=False`` is the default and
is stated here so a reader sees it); every decision — retry, refusal, digest —
lives in ``HandlerStackCompose``. A command that times out raises
``subprocess.TimeoutExpired``; the handler turns any runner exception into a
failed step result, so this adapter never swallows one.

Related:
    - OMN-23968: stability stack compose effect node.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence

from omnimarket.nodes.node_stability_stack_compose_effect.models.model_stack_compose import (
    ModelCommandOutput,
)

__all__ = ["AdapterSubprocessRunner"]


class AdapterSubprocessRunner:
    """Runs one argv list via ``subprocess.run`` and returns ``ModelCommandOutput``.

    Implements :class:`~omnimarket.nodes.node_stability_stack_compose_effect.protocols.protocol_stack_command_runner.ProtocolStackCommandRunner`
    structurally (``run(argv, env, timeout_seconds) -> ModelCommandOutput``).
    """

    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout_seconds: float,
    ) -> ModelCommandOutput:
        """Execute ``argv`` with ``env`` and ``timeout_seconds``, never via a shell."""
        completed = subprocess.run(
            list(argv),
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,  # argv only; the handler never builds a shell string.
        )
        # A signal-terminated child reports ``None``; the typed model wants int.
        returncode = completed.returncode if completed.returncode is not None else -1
        return ModelCommandOutput(
            returncode=returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
