# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a worker may be told to do: the reducer's agent actions, named for the worker (OMN-26721).

A worker is given work only a model can do. Proving, posting and handing off are node
actions the orchestrator takes itself, so none of them has a kind here and an agent
intent the table does not name stops the fold instead of reaching a worker.
"""

from __future__ import annotations

from typing import Final

AGENT_ACTION_KINDS: Final[frozenset[str]] = frozenset(
    {
        "read_failing_log",
        "five_whys",
        "fix",
        "merge_base",
        "merge_head",
        "commit",
        "push",
    }
)

# The reducer's action name to the kind the worker receives.
_KIND_OF_ACTION: Final[dict[str, str]] = {
    "read_failing_log": "read_failing_log",
    "read_proof_log": "read_failing_log",
    "five_whys_on_log": "five_whys",
    "five_whys_on_raw_result": "five_whys",
    "fix_named_cause": "fix",
    "fix_refused_check": "fix",
    "fix_hook_finding": "fix",
    "fix_help_cause": "fix",
    "merge_base_and_commit": "merge_base",
    "merge_moved_head": "merge_head",
    "commit_again": "commit",
    "push_through_guard": "push",
}


def worker_kind(action_name: str) -> str:
    """The kind of the agent action ``action_name``; an unnamed one is a defect."""
    try:
        return _KIND_OF_ACTION[action_name]
    except KeyError as error:
        msg = f"agent action {action_name!r} has no worker kind in {sorted(AGENT_ACTION_KINDS)}"
        raise KeyError(msg) from error


__all__: list[str] = ["AGENT_ACTION_KINDS", "worker_kind"]
