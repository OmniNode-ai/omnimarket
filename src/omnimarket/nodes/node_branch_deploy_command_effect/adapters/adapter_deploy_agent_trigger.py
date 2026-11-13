# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The subprocess adapter for the deploy agent trigger."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence

from omnimarket.nodes.node_branch_deploy_command_effect.protocols.protocol_deploy_agent_trigger import (
    ModelTriggerOutcome,
)


def _parse_env_file(env_file: str) -> dict[str, str]:
    """Read KEY=VALUE lines of env_file into a dict, ignoring blanks and comments."""
    env: dict[str, str] = {}
    with open(env_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            env[key] = value
    return env


class SubprocessDeployAgentTrigger:
    """Runs the deploy agent trigger as a subprocess."""

    def run(
        self,
        argv: Sequence[str],
        env_file: str,
        timeout_seconds: int,
    ) -> ModelTriggerOutcome:
        child_env = dict(os.environ)
        child_env.update(_parse_env_file(env_file))
        try:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=child_env,
            )
            return ModelTriggerOutcome(
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            return ModelTriggerOutcome(
                exit_code=-1,
                stdout="",
                stderr=type(exc).__name__,
            )
        except OSError as exc:
            return ModelTriggerOutcome(
                exit_code=-1,
                stdout="",
                stderr=type(exc).__name__,
            )
