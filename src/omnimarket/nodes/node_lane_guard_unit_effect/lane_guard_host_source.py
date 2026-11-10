# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane guard's host program as text, for the start nodes to inject (OMN-23502).

The guard ships to a lab host as one stdlib-only program in a shell variable (``python3 -c "$G_SRC" reap ...``), so
the host needs nothing installed. The ssh transport of the lane start takes that text as ``guard_host_source``; this
reads it from the program beside the contract, the single copy omnimarket owns, so no second copy exists to drift.
"""

from pathlib import Path

HOST_PROGRAM = "runtime_lane_guard_host.py"
#: The commands the start and close paths send the program (the transport's reap and drain scripts).
REQUIRED_COMMANDS = ("reap", "spool-drain")


def lane_guard_host_source() -> str:
    """The guard host program's text; refuses a program that is empty or no longer carries a command the nodes send."""
    text = (Path(__file__).resolve().parent / HOST_PROGRAM).read_text(encoding="utf-8")
    missing = [c for c in REQUIRED_COMMANDS if f'"{c}"' not in text]
    if not text.strip() or missing:
        raise ValueError(
            f"lane guard host program {HOST_PROGRAM}: missing command(s) {missing or list(REQUIRED_COMMANDS)}"
        )
    return text


__all__ = ["HOST_PROGRAM", "REQUIRED_COMMANDS", "lane_guard_host_source"]
