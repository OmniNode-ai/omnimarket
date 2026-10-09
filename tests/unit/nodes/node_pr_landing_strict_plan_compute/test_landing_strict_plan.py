# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing strict plan compute: the order of a tick's merges and branch updates on a strict base.

The cases below are the ones the live controller's own ``strict_plan`` was run on beside this node (the
same decision actions, live reads, base settings, strict-slot memory, declared fixes, M4 PRs and clock);
the expected values are what it returned: the ranked order, the deferred updates, the held actions, the
fixes holding each base, the next strict-slot view, and the bases it looked up.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_pr_landing_strict_plan_compute import (
    HandlerPrLandingStrictPlan,
    NodePrLandingStrictPlanCompute,
    plan_strict_order,
)
from omnimarket.nodes.node_pr_landing_strict_plan_compute.handlers import (
    handler_pr_landing_strict_plan,
)
from omnimarket.nodes.node_pr_landing_strict_plan_compute.models.model_landing_strict_plan import (
    ModelLandingStrictPlanRequest,
    ModelLandingStrictPlanResult,
)

NODE_DIR = Path(handler_pr_landing_strict_plan.__file__).resolve().parents[1]

CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    (
        "one_action_per_base",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "merge", "subject": "OmniNode-ai/omniclaude#2"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omniclaude#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {},
            "bases_to_read": [],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0, 1],
            "ordered": {},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "two_merges_on_a_strict_base",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#2"},
                {"kind": "kill_worker", "subject": "OmniNode-ai/omnimarket#9"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#9": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000009",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0, 1, 2],
            "ordered": {"omnimarket:dev": [0, 1]},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "two_merges_on_a_base_that_is_not_strict",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#2"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {"omnimarket:dev": False},
        },
        {
            "base_of": {},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0, 1],
            "ordered": {},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "an_unread_base_is_ordered_like_a_strict_one",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#3"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#3": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000003",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev", 2: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {1: "omnimarket:dev"},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0, 1, 2],
            "ordered": {"omnimarket:dev": [0, 1, 2]},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "one_merge_and_three_updates_take_one_slot",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#3"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#4"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#3": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000003",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#4": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000004",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {
                0: "omnimarket:dev",
                1: "omnimarket:dev",
                2: "omnimarket:dev",
                3: "omnimarket:dev",
            },
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {2: "omnimarket:dev", 3: "omnimarket:dev"},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0, 1, 2, 3],
            "ordered": {"omnimarket:dev": [0, 1, 2, 3]},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "two_merges_leave_no_slot_for_an_update",
        {
            "actions": [
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#4"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#2"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#4": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000004",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev", 2: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {0: "omnimarket:dev"},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0, 1, 2],
            "ordered": {"omnimarket:dev": [0, 1, 2]},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "updates_only_take_the_cap",
        {
            "actions": [
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#3"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#3": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000003",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {1: "omnimarket:dev"},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0, 1],
            "ordered": {"omnimarket:dev": [0, 1]},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "a_cause_fix_that_acts_holds_the_base",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#3"},
            ],
            "cause_fix_claims": [],
            "fix_prs": ["omnimarket#3"],
            "fix_why": {"omnimarket#3": "cause lease L12"},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#3": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000003",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev", 2: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {1: "omnimarket:dev"},
            "details": {
                0: "strict base omnimarket:dev: cause fix omnimarket#3 goes first (cause lease L12)",
                1: "strict base omnimarket:dev: cause fix omnimarket#3 goes first (cause lease "
                "L12)",
            },
            "fixes": {"omnimarket#3": "cause lease L12"},
            "held": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "holding": {"omnimarket:dev": ["omnimarket#3"]},
            "order": [2, 0, 1],
            "ordered": {"omnimarket:dev": [2, 0, 1]},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "a_cause_fix_with_young_pending_checks_holds_the_base",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"},
            ],
            "cause_fix_claims": [],
            "fix_prs": ["omnimarket#7"],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#7": {
                    "base": "dev",
                    "head": "7777777777777777777777777777777777777777",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "PENDING",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#7": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": [],
                    "from": None,
                    "pending": {
                        "head": "7777777777777777777777777777777777777777",
                        "since": "2026-10-09T11:50:00Z",
                    },
                    "strikes": [],
                    "yielded": None,
                }
            },
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {1: "omnimarket:dev"},
            "details": {
                0: "strict base omnimarket:dev: cause fix omnimarket#7 goes first (cause lease)",
                1: "strict base omnimarket:dev: cause fix omnimarket#7 goes first (cause lease)",
            },
            "fixes": {"omnimarket#7": "cause lease"},
            "held": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "holding": {"omnimarket:dev": ["omnimarket#7"]},
            "order": [0, 1],
            "ordered": {"omnimarket:dev": [0, 1]},
            "slot": {
                "omnimarket#7": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": [],
                    "from": None,
                    "pending": {
                        "head": "7777777777777777777777777777777777777777",
                        "since": "2026-10-09T11:50:00Z",
                    },
                    "strikes": [],
                    "yielded": None,
                }
            },
            "yielded": {},
        },
    ),
    (
        "a_cause_fix_pending_over_the_hour_holds_nothing",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"},
            ],
            "cause_fix_claims": [],
            "fix_prs": ["omnimarket#7"],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#7": {
                    "base": "dev",
                    "head": "7777777777777777777777777777777777777777",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "PENDING",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#7": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": [],
                    "from": None,
                    "pending": {
                        "head": "7777777777777777777777777777777777777777",
                        "since": "2026-10-09T10:00:00Z",
                    },
                    "strikes": [],
                    "yielded": None,
                }
            },
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {},
            "details": {},
            "fixes": {"omnimarket#7": "cause lease"},
            "held": {},
            "holding": {},
            "order": [0, 1],
            "ordered": {"omnimarket:dev": [0, 1]},
            "slot": {
                "omnimarket#7": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": [],
                    "from": None,
                    "pending": {
                        "head": "7777777777777777777777777777777777777777",
                        "since": "2026-10-09T10:00:00Z",
                    },
                    "strikes": [],
                    "yielded": None,
                }
            },
            "yielded": {},
        },
    ),
    (
        "a_fix_declared_on_a_claim_holds_the_base",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"},
            ],
            "cause_fix_claims": [
                {"cause": "c1", "lane": "landing-cause-L9", "pr": "omnimarket#7"}
            ],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#7": {
                    "base": "dev",
                    "head": "7777777777777777777777777777777777777777",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "PENDING",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#7": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": [],
                    "from": None,
                    "pending": {
                        "head": "7777777777777777777777777777777777777777",
                        "since": "2026-10-09T11:55:00Z",
                    },
                    "strikes": [],
                    "yielded": None,
                }
            },
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {1: "omnimarket:dev"},
            "details": {
                0: "strict base omnimarket:dev: cause fix omnimarket#7 goes first (cause CLAIM "
                "lane=landing-cause-L9 cause=c1)",
                1: "strict base omnimarket:dev: cause fix omnimarket#7 goes first (cause CLAIM "
                "lane=landing-cause-L9 cause=c1)",
            },
            "fixes": {"omnimarket#7": "cause CLAIM lane=landing-cause-L9 cause=c1"},
            "held": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "holding": {"omnimarket:dev": ["omnimarket#7"]},
            "order": [0, 1],
            "ordered": {"omnimarket:dev": [0, 1]},
            "slot": {
                "omnimarket#7": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": [],
                    "from": None,
                    "pending": {
                        "head": "7777777777777777777777777777777777777777",
                        "since": "2026-10-09T11:55:00Z",
                    },
                    "strikes": [],
                    "yielded": None,
                }
            },
            "yielded": {},
        },
    ),
    (
        "a_conflicting_or_closed_fix_holds_nothing",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#2"},
            ],
            "cause_fix_claims": [],
            "fix_prs": ["omnimarket#7", "omnimarket#8", "omnimarket#6", "omnimarket#5"],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#6": {
                    "base": "dev",
                    "head": "6666666666666666666666666666666666666666",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "CLOSED",
                },
                "omnimarket#7": {
                    "base": "dev",
                    "head": "7777777777777777777777777777777777777777",
                    "merge_state": "DIRTY",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#8": {
                    "base": "dev",
                    "head": "8888888888888888888888888888888888888888",
                    "merge_state": "CLEAN",
                    "mergeable": "CONFLICTING",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {},
            "details": {},
            "fixes": {
                "omnimarket#5": "cause lease",
                "omnimarket#6": "cause lease",
                "omnimarket#7": "cause lease",
                "omnimarket#8": "cause lease",
            },
            "held": {},
            "holding": {},
            "order": [0, 1],
            "ordered": {"omnimarket:dev": [0, 1]},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "a_pr_that_failed_two_fresh_heads_yields_the_update_slot",
        {
            "actions": [
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#3"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "FAILURE",
                    "state": "OPEN",
                },
                "omnimarket#3": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000003",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#2": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": [
                        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "0000000000000000000000000000000000000002",
                    ],
                    "from": None,
                    "pending": None,
                    "strikes": ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
                    "yielded": None,
                }
            },
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev", 2: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {0: "omnimarket:dev"},
            "details": {
                0: "strict base omnimarket:dev: omnimarket#2 yields the update slot: ci failed or "
                "was cancelled on its fresh heads aaaaaaa, 0000000; it takes the slot again when "
                "its head changes"
            },
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [1, 2, 0],
            "ordered": {"omnimarket:dev": [1, 2, 0]},
            "slot": {
                "omnimarket#2": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": [
                        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "0000000000000000000000000000000000000002",
                    ],
                    "from": None,
                    "pending": None,
                    "strikes": [
                        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "0000000000000000000000000000000000000002",
                    ],
                    "yielded": "0000000000000000000000000000000000000002",
                }
            },
            "yielded": {
                "omnimarket#2": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": [
                        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "0000000000000000000000000000000000000002",
                    ],
                    "from": None,
                    "pending": None,
                    "strikes": [
                        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "0000000000000000000000000000000000000002",
                    ],
                    "yielded": "0000000000000000000000000000000000000002",
                }
            },
        },
    ),
    (
        "a_yielded_pr_that_passes_takes_the_slot_again",
        {
            "actions": [
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#3"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#3": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000003",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#2": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": ["0000000000000000000000000000000000000002"],
                    "from": None,
                    "pending": None,
                    "strikes": ["0000000000000000000000000000000000000002"],
                    "yielded": "0000000000000000000000000000000000000002",
                }
            },
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {1: "omnimarket:dev"},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0, 1],
            "ordered": {"omnimarket:dev": [0, 1]},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "a_yielded_pr_acting_alone_orders_its_base",
        {
            "actions": [
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"}
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "FAILURE",
                    "state": "OPEN",
                }
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#2": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": ["0000000000000000000000000000000000000002"],
                    "from": None,
                    "pending": None,
                    "strikes": ["0000000000000000000000000000000000000002"],
                    "yielded": "0000000000000000000000000000000000000002",
                }
            },
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {0: "omnimarket:dev"},
            "details": {
                0: "strict base omnimarket:dev: omnimarket#2 yields the update slot: ci failed or "
                "was cancelled on its fresh heads 0000000; it takes the slot again when its head "
                "changes"
            },
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0],
            "ordered": {"omnimarket:dev": [0]},
            "slot": {
                "omnimarket#2": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": ["0000000000000000000000000000000000000002"],
                    "from": None,
                    "pending": None,
                    "strikes": ["0000000000000000000000000000000000000002"],
                    "yielded": "0000000000000000000000000000000000000002",
                }
            },
            "yielded": {
                "omnimarket#2": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": ["0000000000000000000000000000000000000002"],
                    "from": None,
                    "pending": None,
                    "strikes": ["0000000000000000000000000000000000000002"],
                    "yielded": "0000000000000000000000000000000000000002",
                }
            },
        },
    ),
    (
        "the_update_we_made_gives_a_fresh_head",
        {
            "actions": [{"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"}],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "FAILURE",
                    "state": "OPEN",
                }
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#1": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": ["bbbbbbb"],
                    "from": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "pending": None,
                    "strikes": [],
                    "yielded": None,
                },
                "omnimarket#4": {
                    "at": "2026-10-07T12:00:00Z",
                    "fresh": [],
                    "from": "cccccccccccccccccccccccccccccccccccccccc",
                    "pending": None,
                    "strikes": [],
                    "yielded": None,
                },
                "omnimarket#5": {
                    "at": "2026-10-01T12:00:00Z",
                    "fresh": [],
                    "from": "cccccccccccccccccccccccccccccccccccccccc",
                    "pending": None,
                    "strikes": [],
                    "yielded": None,
                },
                "omnimarket#6": {
                    "at": "x",
                    "fresh": [],
                    "from": None,
                    "pending": None,
                    "strikes": [],
                    "yielded": None,
                },
            },
            "strict_of": {},
        },
        {
            "base_of": {},
            "bases_to_read": [],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0],
            "ordered": {},
            "slot": {
                "omnimarket#1": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": ["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
                    "from": None,
                    "pending": None,
                    "strikes": ["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
                    "yielded": None,
                },
                "omnimarket#4": {
                    "at": "2026-10-07T12:00:00Z",
                    "fresh": [],
                    "from": "cccccccccccccccccccccccccccccccccccccccc",
                    "pending": None,
                    "strikes": [],
                    "yielded": None,
                },
            },
            "yielded": {},
        },
    ),
    (
        "a_foreign_push_clears_the_strikes",
        {
            "actions": [{"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"}],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "dddddddddddddddddddddddddddddddddddddddd",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "PENDING",
                    "state": "OPEN",
                }
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#1": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": ["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
                    "from": None,
                    "pending": None,
                    "strikes": ["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
                    "yielded": None,
                }
            },
            "strict_of": {},
        },
        {
            "base_of": {},
            "bases_to_read": [],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0],
            "ordered": {},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "m4_pr_actions_go_first_outside_a_strict_base",
        {
            "actions": [
                {"kind": "dispatch_worker", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "merge", "subject": "OmniNode-ai/omniclaude#2"},
                {"kind": "merge", "subject": "OmniNode-ai/omnidash#3"},
                {"kind": "dispatch_worker", "subject": "OmniNode-ai/omnimarket#4"},
                {"kind": "kill_worker", "subject": "OmniNode-ai/omnimarket#5"},
                {"kind": "rerun", "subject": "OmniNode-ai/omnimarket#6"},
                {"kind": "eligibility_rerun", "subject": "OmniNode-ai/omnimarket#7"},
                {"kind": "rerun", "subject": "OmniNode-ai/omnimarket#8"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omniclaude#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnidash#3": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000003",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#4": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000004",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#5": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000005",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#6": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000006",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#7": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000007",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#8": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000008",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [
                "omnimarket#4",
                "omnidash#3",
                "omnimarket#8",
                "omnimarket#7",
                "omnimarket#5",
            ],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {},
        },
        {
            "base_of": {},
            "bases_to_read": [],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [3, 2, 1, 0, 4, 7, 6, 5],
            "ordered": {},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "m4_pr_ranks_after_the_fix_on_a_strict_base",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#2"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#3"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#4"},
                {"kind": "dispatch_worker", "subject": "OmniNode-ai/omnimarket#5"},
                {"kind": "dispatch_worker", "subject": "OmniNode-ai/omnimarket#6"},
            ],
            "cause_fix_claims": [],
            "fix_prs": ["omnimarket#2"],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#3": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000003",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#4": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000004",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#5": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000005",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#6": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000006",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": ["omnimarket#3", "omnimarket#6"],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {
                0: "omnimarket:dev",
                1: "omnimarket:dev",
                2: "omnimarket:dev",
                3: "omnimarket:dev",
            },
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {3: "omnimarket:dev"},
            "details": {
                0: "strict base omnimarket:dev: cause fix omnimarket#2 goes first (cause lease)",
                2: "strict base omnimarket:dev: cause fix omnimarket#2 goes first (cause lease)",
                3: "strict base omnimarket:dev: cause fix omnimarket#2 goes first (cause lease)",
            },
            "fixes": {"omnimarket#2": "cause lease"},
            "held": {0: "omnimarket:dev", 2: "omnimarket:dev", 3: "omnimarket:dev"},
            "holding": {"omnimarket:dev": ["omnimarket#2"]},
            "order": [1, 2, 0, 3, 5, 4],
            "ordered": {"omnimarket:dev": [1, 2, 0, 3]},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "two_bases_each_ordered_in_its_own_positions",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "merge", "subject": "OmniNode-ai/omniclaude#2"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#3"},
                {"kind": "merge", "subject": "OmniNode-ai/omniclaude#4"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#5"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omniclaude#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omniclaude#4": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000004",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#3": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000003",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#5": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000005",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": ["omniclaude#4"],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {"omniclaude:dev": True, "omnimarket:dev": True},
        },
        {
            "base_of": {
                0: "omnimarket:dev",
                1: "omniclaude:dev",
                2: "omnimarket:dev",
                3: "omniclaude:dev",
                4: "omnimarket:dev",
            },
            "bases_to_read": ["omniclaude:dev", "omnimarket:dev"],
            "deferred_updates": {4: "omnimarket:dev"},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0, 3, 2, 1, 4],
            "ordered": {"omniclaude:dev": [3, 1], "omnimarket:dev": [0, 2, 4]},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "an_action_on_a_pr_with_no_live_read_or_no_base",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#2"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#3"},
                {"kind": "close", "subject": "OmniNode-ai/omnimarket#4"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "",
                    "head": "1111111111111111111111111111111111111111",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "",
                    "head": "2222222222222222222222222222222222222222",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {},
        },
        {
            "base_of": {},
            "bases_to_read": [],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0, 1, 2, 3],
            "ordered": {},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "no_actions",
        {
            "actions": [],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {},
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {},
        },
        {
            "base_of": {},
            "bases_to_read": [],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [],
            "ordered": {},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "a_fix_pending_for_3599_seconds_still_holds",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"},
            ],
            "cause_fix_claims": [],
            "fix_prs": ["omnimarket#7"],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#7": {
                    "base": "dev",
                    "head": "7777777777777777777777777777777777777777",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "PENDING",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#7": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": [],
                    "from": None,
                    "pending": {
                        "head": "7777777777777777777777777777777777777777",
                        "since": "2026-10-09T11:00:01Z",
                    },
                    "strikes": [],
                    "yielded": None,
                }
            },
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {1: "omnimarket:dev"},
            "details": {
                0: "strict base omnimarket:dev: cause fix omnimarket#7 goes first (cause lease)",
                1: "strict base omnimarket:dev: cause fix omnimarket#7 goes first (cause lease)",
            },
            "fixes": {"omnimarket#7": "cause lease"},
            "held": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "holding": {"omnimarket:dev": ["omnimarket#7"]},
            "order": [0, 1],
            "ordered": {"omnimarket:dev": [0, 1]},
            "slot": {
                "omnimarket#7": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": [],
                    "from": None,
                    "pending": {
                        "head": "7777777777777777777777777777777777777777",
                        "since": "2026-10-09T11:00:01Z",
                    },
                    "strikes": [],
                    "yielded": None,
                }
            },
            "yielded": {},
        },
    ),
    (
        "a_fix_pending_for_3600_seconds_holds_nothing",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"},
            ],
            "cause_fix_claims": [],
            "fix_prs": ["omnimarket#7"],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#7": {
                    "base": "dev",
                    "head": "7777777777777777777777777777777777777777",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "PENDING",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#7": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": [],
                    "from": None,
                    "pending": {
                        "head": "7777777777777777777777777777777777777777",
                        "since": "2026-10-09T11:00:00Z",
                    },
                    "strikes": [],
                    "yielded": None,
                }
            },
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {},
            "details": {},
            "fixes": {"omnimarket#7": "cause lease"},
            "held": {},
            "holding": {},
            "order": [0, 1],
            "ordered": {"omnimarket:dev": [0, 1]},
            "slot": {
                "omnimarket#7": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": [],
                    "from": None,
                    "pending": {
                        "head": "7777777777777777777777777777777777777777",
                        "since": "2026-10-09T11:00:00Z",
                    },
                    "strikes": [],
                    "yielded": None,
                }
            },
            "yielded": {},
        },
    ),
    (
        "slot_memory_of_a_pr_gone_from_the_read_is_kept_just_under_a_week",
        {
            "actions": [],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {},
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#4": {
                    "at": "2026-10-03T00:00:00Z",
                    "fresh": [],
                    "from": "cccccccccccccccccccccccccccccccccccccccc",
                    "pending": None,
                    "strikes": [],
                    "yielded": None,
                },
                "omnimarket#5": {
                    "at": "2026-10-02T12:00:00Z",
                    "fresh": [],
                    "from": "cccccccccccccccccccccccccccccccccccccccc",
                    "pending": None,
                    "strikes": [],
                    "yielded": None,
                },
                "omnimarket#6": {
                    "at": "2026-10-02T12:00:01Z",
                    "fresh": [],
                    "from": "cccccccccccccccccccccccccccccccccccccccc",
                    "pending": None,
                    "strikes": [],
                    "yielded": None,
                },
            },
            "strict_of": {},
        },
        {
            "base_of": {},
            "bases_to_read": [],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [],
            "ordered": {},
            "slot": {
                "omnimarket#4": {
                    "at": "2026-10-03T00:00:00Z",
                    "fresh": [],
                    "from": "cccccccccccccccccccccccccccccccccccccccc",
                    "pending": None,
                    "strikes": [],
                    "yielded": None,
                },
                "omnimarket#6": {
                    "at": "2026-10-02T12:00:01Z",
                    "fresh": [],
                    "from": "cccccccccccccccccccccccccccccccccccccccc",
                    "pending": None,
                    "strikes": [],
                    "yielded": None,
                },
            },
            "yielded": {},
        },
    ),
    (
        "only_the_last_five_fresh_heads_are_kept",
        {
            "actions": [],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "9999999999999999999999999999999999999999",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "PENDING",
                    "state": "OPEN",
                }
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#1": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": [
                        "1111111111111111111111111111111111111111",
                        "2222222222222222222222222222222222222222",
                        "3333333333333333333333333333333333333333",
                        "4444444444444444444444444444444444444444",
                        "5555555555555555555555555555555555555555",
                        "6666666666666666666666666666666666666666",
                        "7777777777777777777777777777777777777777",
                        "8888888888888888888888888888888888888888",
                    ],
                    "from": None,
                    "pending": None,
                    "strikes": [],
                    "yielded": None,
                }
            },
            "strict_of": {},
        },
        {
            "base_of": {},
            "bases_to_read": [],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [],
            "ordered": {},
            "slot": {
                "omnimarket#1": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": [
                        "4444444444444444444444444444444444444444",
                        "5555555555555555555555555555555555555555",
                        "6666666666666666666666666666666666666666",
                        "7777777777777777777777777777777777777777",
                        "8888888888888888888888888888888888888888",
                    ],
                    "from": None,
                    "pending": None,
                    "strikes": [],
                    "yielded": None,
                }
            },
            "yielded": {},
        },
    ),
    (
        "a_closed_fix_that_acts_holds_nothing",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#7"},
            ],
            "cause_fix_claims": [],
            "fix_prs": ["omnimarket#7"],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#7": {
                    "base": "dev",
                    "head": "7777777777777777777777777777777777777777",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "CLOSED",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {},
            "details": {},
            "fixes": {"omnimarket#7": "cause lease"},
            "held": {},
            "holding": {},
            "order": [1, 0],
            "ordered": {"omnimarket:dev": [1, 0]},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "a_conflicting_fix_that_acts_holds_nothing",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#7"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#8"},
            ],
            "cause_fix_claims": [],
            "fix_prs": ["omnimarket#7", "omnimarket#8"],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#7": {
                    "base": "dev",
                    "head": "7777777777777777777777777777777777777777",
                    "merge_state": "CLEAN",
                    "mergeable": "CONFLICTING",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#8": {
                    "base": "dev",
                    "head": "8888888888888888888888888888888888888888",
                    "merge_state": "DIRTY",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev", 2: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {2: "omnimarket:dev"},
            "details": {},
            "fixes": {"omnimarket#7": "cause lease", "omnimarket#8": "cause lease"},
            "held": {},
            "holding": {},
            "order": [1, 2, 0],
            "ordered": {"omnimarket:dev": [1, 2, 0]},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "merges_held_for_a_fix_leave_the_slot_to_the_fixs_update",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#2"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#7"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#3"},
            ],
            "cause_fix_claims": [],
            "fix_prs": ["omnimarket#7"],
            "fix_why": {"omnimarket#7": "cause lease L3"},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#3": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000003",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#7": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000007",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {
                0: "omnimarket:dev",
                1: "omnimarket:dev",
                2: "omnimarket:dev",
                3: "omnimarket:dev",
            },
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {3: "omnimarket:dev"},
            "details": {
                0: "strict base omnimarket:dev: cause fix omnimarket#7 goes first (cause lease L3)",
                1: "strict base omnimarket:dev: cause fix omnimarket#7 goes first (cause lease L3)",
                3: "strict base omnimarket:dev: cause fix omnimarket#7 goes first (cause lease L3)",
            },
            "fixes": {"omnimarket#7": "cause lease L3"},
            "held": {0: "omnimarket:dev", 1: "omnimarket:dev", 3: "omnimarket:dev"},
            "holding": {"omnimarket:dev": ["omnimarket#7"]},
            "order": [2, 0, 1, 3],
            "ordered": {"omnimarket:dev": [2, 0, 1, 3]},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "kill_worker_actions_are_never_reordered_for_m4",
        {
            "actions": [
                {"kind": "kill_worker", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "kill_worker", "subject": "OmniNode-ai/omnimarket#2"},
                {"kind": "close", "subject": "OmniNode-ai/omnimarket#3"},
                {"kind": "close", "subject": "OmniNode-ai/omnimarket#4"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#3": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000003",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#4": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000004",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": ["omnimarket#2", "omnimarket#4"],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {},
            "strict_of": {},
        },
        {
            "base_of": {},
            "bases_to_read": [],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [0, 1, 2, 3],
            "ordered": {},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "a_head_that_is_where_our_update_began_keeps_its_strikes",
        {
            "actions": [],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "PENDING",
                    "state": "OPEN",
                }
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#1": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": ["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
                    "from": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "pending": None,
                    "strikes": ["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
                    "yielded": None,
                }
            },
            "strict_of": {},
        },
        {
            "base_of": {},
            "bases_to_read": [],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [],
            "ordered": {},
            "slot": {
                "omnimarket#1": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": ["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
                    "from": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "pending": None,
                    "strikes": ["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
                    "yielded": None,
                }
            },
            "yielded": {},
        },
    ),
    (
        "a_pass_on_a_fresh_head_clears_its_strikes",
        {
            "actions": [],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                }
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#1": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": ["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
                    "from": None,
                    "pending": None,
                    "strikes": ["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
                    "yielded": None,
                }
            },
            "strict_of": {},
        },
        {
            "base_of": {},
            "bases_to_read": [],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [],
            "ordered": {},
            "slot": {
                "omnimarket#1": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": ["bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
                    "from": None,
                    "pending": None,
                    "strikes": [],
                    "yielded": None,
                }
            },
            "yielded": {},
        },
    ),
    (
        "a_fix_whose_head_moved_restarts_the_pending_clock",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"},
            ],
            "cause_fix_claims": [],
            "fix_prs": ["omnimarket#7"],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#7": {
                    "base": "dev",
                    "head": "8888888888888888888888888888888888888888",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "PENDING",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#7": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": [],
                    "from": None,
                    "pending": {
                        "head": "7777777777777777777777777777777777777777",
                        "since": "2026-10-09T09:00:00Z",
                    },
                    "strikes": [],
                    "yielded": None,
                }
            },
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {1: "omnimarket:dev"},
            "details": {
                0: "strict base omnimarket:dev: cause fix omnimarket#7 goes first (cause lease)",
                1: "strict base omnimarket:dev: cause fix omnimarket#7 goes first (cause lease)",
            },
            "fixes": {"omnimarket#7": "cause lease"},
            "held": {0: "omnimarket:dev", 1: "omnimarket:dev"},
            "holding": {"omnimarket:dev": ["omnimarket#7"]},
            "order": [0, 1],
            "ordered": {"omnimarket:dev": [0, 1]},
            "slot": {
                "omnimarket#7": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": [],
                    "from": None,
                    "pending": {
                        "head": "8888888888888888888888888888888888888888",
                        "since": "2026-10-09T12:00:00Z",
                    },
                    "strikes": [],
                    "yielded": None,
                }
            },
            "yielded": {},
        },
    ),
    (
        "a_fix_that_stops_pending_loses_its_clock",
        {
            "actions": [{"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"}],
            "cause_fix_claims": [],
            "fix_prs": ["omnimarket#7"],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#7": {
                    "base": "dev",
                    "head": "7777777777777777777777777777777777777777",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#7": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": [],
                    "from": None,
                    "pending": {
                        "head": "7777777777777777777777777777777777777777",
                        "since": "2026-10-09T11:57:00Z",
                    },
                    "strikes": [],
                    "yielded": None,
                }
            },
            "strict_of": {},
        },
        {
            "base_of": {},
            "bases_to_read": [],
            "deferred_updates": {},
            "details": {},
            "fixes": {"omnimarket#7": "cause lease"},
            "held": {},
            "holding": {},
            "order": [0],
            "ordered": {},
            "slot": {},
            "yielded": {},
        },
    ),
    (
        "a_yielded_prs_merge_does_not_use_up_the_update_slot",
        {
            "actions": [
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#2"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#3"},
            ],
            "cause_fix_claims": [],
            "fix_prs": [],
            "fix_why": {},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "FAILURE",
                    "state": "OPEN",
                },
                "omnimarket#3": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000003",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#2": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": ["0000000000000000000000000000000000000002"],
                    "from": None,
                    "pending": None,
                    "strikes": ["0000000000000000000000000000000000000002"],
                    "yielded": "0000000000000000000000000000000000000002",
                }
            },
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev", 2: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {},
            "details": {},
            "fixes": {},
            "held": {},
            "holding": {},
            "order": [1, 2, 0],
            "ordered": {"omnimarket:dev": [1, 2, 0]},
            "slot": {
                "omnimarket#2": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": ["0000000000000000000000000000000000000002"],
                    "from": None,
                    "pending": None,
                    "strikes": ["0000000000000000000000000000000000000002"],
                    "yielded": "0000000000000000000000000000000000000002",
                }
            },
            "yielded": {
                "omnimarket#2": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": ["0000000000000000000000000000000000000002"],
                    "from": None,
                    "pending": None,
                    "strikes": ["0000000000000000000000000000000000000002"],
                    "yielded": "0000000000000000000000000000000000000002",
                }
            },
        },
    ),
    (
        "a_declared_fix_that_yielded_the_slot_ranks_last",
        {
            "actions": [
                {"kind": "update_branch", "subject": "OmniNode-ai/omnimarket#2"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#1"},
                {"kind": "merge", "subject": "OmniNode-ai/omnimarket#3"},
            ],
            "cause_fix_claims": [],
            "fix_prs": ["omnimarket#2"],
            "fix_why": {"omnimarket#2": "cause lease L4"},
            "live": {
                "omnimarket#1": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000001",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
                "omnimarket#2": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000002",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "FAILURE",
                    "state": "OPEN",
                },
                "omnimarket#3": {
                    "base": "dev",
                    "head": "0000000000000000000000000000000000000003",
                    "merge_state": "CLEAN",
                    "mergeable": "MERGEABLE",
                    "rollup": "SUCCESS",
                    "state": "OPEN",
                },
            },
            "m4": [],
            "now": "2026-10-09T12:00:00.345678Z",
            "slot_memory": {
                "omnimarket#2": {
                    "at": "2026-10-09T11:00:00Z",
                    "fresh": ["0000000000000000000000000000000000000002"],
                    "from": None,
                    "pending": None,
                    "strikes": ["0000000000000000000000000000000000000002"],
                    "yielded": "0000000000000000000000000000000000000002",
                }
            },
            "strict_of": {"omnimarket:dev": True},
        },
        {
            "base_of": {0: "omnimarket:dev", 1: "omnimarket:dev", 2: "omnimarket:dev"},
            "bases_to_read": ["omnimarket:dev"],
            "deferred_updates": {0: "omnimarket:dev"},
            "details": {
                0: "strict base omnimarket:dev: omnimarket#2 yields the update slot: ci failed or "
                "was cancelled on its fresh heads 0000000; it takes the slot again when its head "
                "changes"
            },
            "fixes": {"omnimarket#2": "cause lease L4"},
            "held": {},
            "holding": {},
            "order": [1, 2, 0],
            "ordered": {"omnimarket:dev": [1, 2, 0]},
            "slot": {
                "omnimarket#2": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": ["0000000000000000000000000000000000000002"],
                    "from": None,
                    "pending": None,
                    "strikes": ["0000000000000000000000000000000000000002"],
                    "yielded": "0000000000000000000000000000000000000002",
                }
            },
            "yielded": {
                "omnimarket#2": {
                    "at": "2026-10-09T12:00:00Z",
                    "fresh": ["0000000000000000000000000000000000000002"],
                    "from": None,
                    "pending": None,
                    "strikes": ["0000000000000000000000000000000000000002"],
                    "yielded": "0000000000000000000000000000000000000002",
                }
            },
        },
    ),
]

CASES_BY_NAME = {c[0]: c for c in CASES}


def _request(raw: dict[str, Any]) -> ModelLandingStrictPlanRequest:
    return ModelLandingStrictPlanRequest.model_validate(raw)


def _listed(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _listed(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_listed(v) for v in value]
    return value


def _live_shape(result: ModelLandingStrictPlanResult) -> dict[str, Any]:
    """The result in the shape the live controller returned it."""
    return _listed(result.model_dump(by_alias=True))


def _plan(raw: dict[str, Any]) -> dict[str, Any]:
    return _live_shape(HandlerPrLandingStrictPlan().handle(_request(raw)))


@pytest.mark.unit
def test_contract_declares_a_pure_compute_with_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_pr_landing_strict_plan_compute"
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["descriptor"]["purity"] == "pure"
    for side, model in (
        ("input_model", ModelLandingStrictPlanRequest),
        ("output_model", ModelLandingStrictPlanResult),
    ):
        declared = contract[side]
        module = importlib.import_module(declared["module"])
        assert getattr(module, declared["name"]) is model
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerPrLandingStrictPlan
    )
    dispatch = contract["runtime_dispatch"]
    assert dispatch["command_topic"].startswith("onex.cmd.omnimarket.")
    assert set(dispatch["terminal_events"]) == {"success", "failure"}


@pytest.mark.unit
def test_handler_is_definition_b() -> None:
    params = list(
        inspect.signature(HandlerPrLandingStrictPlan.handle).parameters.values()
    )
    assert [p.name for p in params] == ["self", "request"]
    assert not inspect.iscoroutinefunction(HandlerPrLandingStrictPlan.handle)
    assert issubclass(NodePrLandingStrictPlanCompute, HandlerPrLandingStrictPlan)
    request = _request({"now": "2026-10-09T12:00:00Z"})
    assert isinstance(
        HandlerPrLandingStrictPlan().handle(request), ModelLandingStrictPlanResult
    )
    assert plan_strict_order(request) == HandlerPrLandingStrictPlan().handle(request)


@pytest.mark.unit
def test_handler_does_no_io() -> None:
    tree = ast.parse(Path(handler_pr_landing_strict_plan.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"os", "subprocess", "socket", "time", "random", "pathlib"}


@pytest.mark.unit
@pytest.mark.parametrize(("name", "raw", "expected"), CASES, ids=[c[0] for c in CASES])
def test_plan_equals_the_live_controllers(
    name: str, raw: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert _plan(raw) == expected


@pytest.mark.unit
def test_a_base_the_tick_acts_on_once_is_not_ordered() -> None:
    plan = _plan(CASES_BY_NAME["one_action_per_base"][1])
    assert plan["ordered"] == {}
    assert plan["bases_to_read"] == []


@pytest.mark.unit
def test_an_unread_base_is_ordered_and_named_for_the_caller_to_read() -> None:
    raw = CASES_BY_NAME["an_unread_base_is_ordered_like_a_strict_one"][1]
    plan = _plan(raw)
    assert plan["bases_to_read"] == ["omnimarket:dev"]
    assert plan["ordered"] == {"omnimarket:dev": [0, 1, 2]}
    known = _plan({**raw, "strict_of": {"omnimarket:dev": False}})
    assert known["ordered"] == {}


@pytest.mark.unit
def test_a_strict_base_takes_one_update_after_its_merges() -> None:
    plan = _plan(CASES_BY_NAME["one_merge_and_three_updates_take_one_slot"][1])
    assert sorted(plan["deferred_updates"]) == [2, 3]
    plan = _plan(CASES_BY_NAME["two_merges_leave_no_slot_for_an_update"][1])
    assert sorted(plan["deferred_updates"]) == [0]


@pytest.mark.unit
def test_a_declared_fix_goes_first_and_holds_the_others() -> None:
    plan = _plan(CASES_BY_NAME["a_cause_fix_that_acts_holds_the_base"][1])
    assert plan["order"][0] == 2
    assert sorted(plan["held"]) == [0, 1]
    assert plan["holding"] == {"omnimarket:dev": ["omnimarket#3"]}
    assert plan["fixes"] == {"omnimarket#3": "cause lease L12"}


@pytest.mark.unit
def test_a_fix_with_no_stated_reason_is_a_cause_lease() -> None:
    raw = dict(CASES_BY_NAME["a_cause_fix_that_acts_holds_the_base"][1])
    raw["fix_why"] = {}
    assert _plan(raw)["fixes"] == {"omnimarket#3": "cause lease"}


@pytest.mark.unit
def test_a_fix_pending_under_the_hold_window_holds_and_over_it_does_not() -> None:
    young = _plan(
        CASES_BY_NAME["a_cause_fix_with_young_pending_checks_holds_the_base"][1]
    )
    old = _plan(CASES_BY_NAME["a_cause_fix_pending_over_the_hour_holds_nothing"][1])
    assert sorted(young["held"]) == [0, 1]
    assert old["held"] == {}
    assert old["holding"] == {}


@pytest.mark.unit
def test_a_pr_that_failed_two_fresh_heads_yields_and_ranks_last() -> None:
    plan = _plan(
        CASES_BY_NAME["a_pr_that_failed_two_fresh_heads_yields_the_update_slot"][1]
    )
    assert list(plan["yielded"]) == ["omnimarket#2"]
    assert plan["ordered"]["omnimarket:dev"][-1] == 0
    assert "yields the update slot" in plan["details"][0]


@pytest.mark.unit
def test_the_m4_class_goes_first_outside_a_strict_base_inside_each_kind() -> None:
    plan = _plan(CASES_BY_NAME["m4_pr_actions_go_first_outside_a_strict_base"][1])
    assert plan["order"] == [3, 2, 1, 0, 4, 7, 6, 5]


@pytest.mark.unit
def test_a_pr_with_no_live_read_never_orders_its_base() -> None:
    plan = _plan(CASES_BY_NAME["an_action_on_a_pr_with_no_live_read_or_no_base"][1])
    assert plan["ordered"] == {}
    assert plan["order"] == [0, 1, 2, 3]


@pytest.mark.unit
def test_slot_memory_of_a_pr_no_longer_live_is_kept_a_week() -> None:
    slot = _plan(CASES_BY_NAME["the_update_we_made_gives_a_fresh_head"][1])["slot"]
    assert "omnimarket#4" in slot
    assert "omnimarket#5" not in slot
    assert "omnimarket#6" not in slot
    assert slot["omnimarket#1"]["fresh"] == ["b" * 40]
    assert slot["omnimarket#1"]["strikes"] == ["b" * 40]


@pytest.mark.unit
def test_the_result_does_not_depend_on_the_order_of_the_mappings() -> None:
    raw = CASES_BY_NAME["two_bases_each_ordered_in_its_own_positions"][1]
    shuffled = {
        **raw,
        "live": dict(reversed(list(raw["live"].items()))),
        "strict_of": dict(reversed(list(raw["strict_of"].items()))),
    }
    assert _plan(shuffled) == _plan(raw)


@pytest.mark.unit
def test_the_request_is_validated() -> None:
    with pytest.raises(ValidationError):
        _request({"now": "2026-10-09T12:00:00Z", "bogus": 1})
    with pytest.raises(ValidationError):
        _request(
            {"now": "2026-10-09T12:00:00Z", "actions": [{"kind": "", "subject": "a#1"}]}
        )
    with pytest.raises(ValidationError):
        _request({"now": "2026-10-09T12:00:00", "actions": []})
