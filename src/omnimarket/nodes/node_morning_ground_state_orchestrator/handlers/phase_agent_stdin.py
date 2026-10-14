# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pool-host entry of a morning phase agent: brief chunks on argv, brief on stdin.

A pool host runs no shell and bounds each argv word, and a phase brief (with the
PR watcher inventory) is far longer than one word, so the lab work unit carries
the brief as bounded words and this file, run as ``python3 <this file>`` in the
unit's checkout, joins them and hands the brief to the headless agent CLI on
stdin. Standard library only: the checkout has no environment of its own.
"""

from __future__ import annotations

import subprocess
import sys

AGENT_ARGV = (
    "claude",
    "-p",
    "--permission-mode",
    "bypassPermissions",
    "--output-format",
    "text",
)


def main(argv: list[str]) -> int:
    """Run the agent on the joined brief; its exit code is the unit's."""
    if len(argv) < 2:
        sys.stderr.write("usage: phase_agent_stdin.py <model> <brief chunk>...\n")
        return 64
    model, chunks = argv[0], argv[1:]
    done = subprocess.run(
        [*AGENT_ARGV, "--model", model], input="".join(chunks), text=True, check=False
    )
    return done.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
