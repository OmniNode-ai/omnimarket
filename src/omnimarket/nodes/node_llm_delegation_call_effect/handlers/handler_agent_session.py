# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Agent-session transport owned by the delegation call effect.

Lab work invokes this file with the standard-library interpreter in its
checkout. Brief chunks stay below the host's argv bound and are joined on
stdin here, at the model-call boundary. Output and the agent's exit status
pass through to the lab-work receipt.
"""

from __future__ import annotations

import subprocess
import sys


class HandlerAgentSession:
    """Run one delegated agent session in the lab unit's checkout."""

    def run(self, model: str, brief: str) -> int:
        """Keep the agent's permission checks and deliver the brief on stdin."""
        done = subprocess.run(
            [
                "claude",
                "-p",
                "--permission-mode",
                "acceptEdits",
                "--output-format",
                "text",
                "--model",
                model,
            ],
            input=brief,
            text=True,
            check=False,
        )
        return done.returncode


def main(argv: list[str]) -> int:
    """The standalone effect entrypoint for a pool host without a venv."""
    if len(argv) < 2:
        sys.stderr.write("usage: handler_agent_session.py <model> <brief chunk>...\n")
        return 64
    return HandlerAgentSession().run(argv[0], "".join(argv[1:]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
