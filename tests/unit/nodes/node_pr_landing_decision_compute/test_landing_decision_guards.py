# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One test per guard of the landing controller model, and per rule beyond it.

``LandingController.tla`` makes every guard a plan rule adds a CONSTANT, and
its lab TLC run has one mutation config per guard (``M_<property>_<guard>``)
that removes the guard and shows the violation it prevents. Each
``test_guard_M_*`` below is named after one of those eighteen mutation configs
and pins the plan behaviour the guard protects, so removing the matching rule
from the decision fails exactly that test. The ``test_rule_*`` tests pin the
rules the model abstracts away (reruns, update-branch, priority, the engine
ladder and pool, escalation parking, the brief classes).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingBriefClass,
    EnumLandingEngine,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    ModelLandingControllerState,
    ModelLandingLease,
)
from tests.unit.nodes.node_pr_landing_decision_compute.landing_world import (
    run_scenario,
    sha,
)

FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "pr_landing_decision"
    / "review_scenarios"
)

RED = {"ci": "red", "red_class": "product", "red_checks": ["unit"]}


def _fixture(name: str) -> dict[str, Any]:
    spec: dict[str, Any] = yaml.safe_load((FIXTURES / name).read_text())
    return spec


def _pr(pr: str, **fields: Any) -> dict[str, Any]:
    return {"pr": pr, **fields}


# --------------------------------------------------------------------------
# The eighteen mutation configs of the model, one test each.


@pytest.mark.unit
def test_guard_m_p1_lease_by_head() -> None:
    """R7: the lease is keyed repo#pr, so a head change never frees the slot."""
    run_scenario(
        {
            "id": "M_P1_lease_by_head",
            "world": {"prs": [_pr("acme/app#1", **RED)]},
            "ticks": [
                {"expect": {"actions": ["dispatch_worker acme/app#1 real_red"]}},
                {
                    "events": [
                        {"worker_push": {"pr": "acme/app#1"}},
                        {
                            "ci": {
                                "pr": "acme/app#1",
                                "result": "red",
                                "red_checks": ["unit"],
                            }
                        },
                    ],
                    "expect": {"actions": [], "lease_count": 1},
                },
                {
                    "events": [{"foreign_rewrite": {"pr": "acme/app#1"}}],
                    "expect": {
                        "actions": [],
                        "lease": {"acme/app#1": {"last_seen": "h3"}},
                    },
                },
            ],
            "end": {"dispatches": {"acme/app#1": 1}},
        }
    )


@pytest.mark.unit
def test_guard_m_p2_token_late() -> None:
    """R6: the token is freed first in the tick after its holder merged or was held."""
    run_scenario(
        {
            "id": "M_P2_token_late",
            "world": {
                "prs": [
                    _pr("acme/app#1", ci="green", runtime=True),
                    _pr("acme/app#2", ci="green", runtime=True),
                    _pr("acme/app#3", ci="pending", runtime=True),
                ]
            },
            "ticks": [
                {"expect": {"actions": ["merge acme/app#1 h1"], "token": "acme/app#1"}},
                {"expect": {"actions": ["merge acme/app#2 h1"], "token": "acme/app#2"}},
                {"expect": {"actions": [], "token": None}},
                {
                    "events": [
                        {"ci": {"pr": "acme/app#3", "result": "green"}},
                        {"hold": {"pr": "acme/app#3"}},
                    ],
                    "expect": {"actions": [], "token": None},
                },
            ],
        }
    )


@pytest.mark.unit
def test_guard_m_p3_keep_revoked() -> None:
    """S12: a late result carrying a revoked lease id is discarded, never a second outcome."""
    run_scenario(
        {
            "id": "M_P3_keep_revoked",
            "world": {"prs": [_pr("acme/app#1", **RED)]},
            "ticks": [
                {"expect": {"actions": ["dispatch_worker acme/app#1 real_red"]}},
                {
                    "events": [
                        {"drain": {"repo": "acme/app"}},
                        {"kill_fails": {"pr": "acme/app#1"}},
                    ],
                    "expect": {
                        "actions": ["kill_worker acme/app#1"],
                        "lease": {"acme/app#1": {"revoked": True}},
                    },
                },
                {
                    "events": [{"process_exit": {"pr": "acme/app#1"}}],
                    "expect": {
                        "actions": ["observe_only acme/app"],
                        "recorded": ["acme/app#1 timed_out revoked"],
                    },
                },
                {
                    "events": [
                        {
                            "worker_result": {
                                "pr": "acme/app#1",
                                "lease": 1,
                                "kind": "fix_submitted",
                            }
                        }
                    ],
                    "expect": {
                        "actions": [],
                        "observed": ["discard_result acme/app#1"],
                        "recorded": [],
                    },
                },
            ],
        }
    )


@pytest.mark.unit
def test_guard_m_p4_member_elig() -> None:
    """R2 rule 4: a green, current companion with a suspended member never merges."""
    run_scenario(
        {
            "id": "M_P4_member_elig",
            "world": {
                "prs": [
                    _pr("acme/app#1", suspensions=["do_not_land"]),
                    _pr("acme/lib#2"),
                ],
                "companions": [
                    {
                        "pr": "acme/occ#1",
                        "members": ["acme/app#1@h1", "acme/lib#2@h1"],
                        "ci": "green",
                    }
                ],
            },
            "ticks": [
                {
                    "expect": {
                        "actions": ["companion_rebuild acme/occ#1 [acme/lib#2@h1]"],
                        "uncovered": ["acme/app#1"],
                    }
                }
            ],
        }
    )


@pytest.mark.unit
def test_guard_m_p4_member_elig_collaborator() -> None:
    """A collaborator's PR is excluded: its companion never merges with it."""
    run_scenario(
        {
            "id": "M_P4_collaborator",
            "world": {
                "prs": [_pr("acme/app#1", collaborator=True), _pr("acme/lib#2")],
                "companions": [
                    {
                        "pr": "acme/occ#1",
                        "members": ["acme/app#1@h1", "acme/lib#2@h1"],
                        "ci": "green",
                    }
                ],
            },
            "ticks": [
                {
                    "expect": {
                        "actions": ["companion_rebuild acme/occ#1 [acme/lib#2@h1]"],
                        "uncovered": [],
                    }
                }
            ],
        }
    )


@pytest.mark.unit
def test_guard_m_p5_close_suspended() -> None:
    """R2 rule 2: no member eligible but one suspended waits; it is never closed."""
    run_scenario(
        {
            "id": "M_P5_close_suspended",
            "world": {
                "prs": [_pr("acme/app#1"), _pr("acme/lib#2", suspensions=["hold"])],
                "companions": [
                    {"pr": "acme/occ#1", "members": ["acme/app#1@h1", "acme/lib#2@h1"]}
                ],
            },
            "ticks": [
                {
                    "events": [{"member_close": {"pr": "acme/app#1"}}],
                    "expect": {"actions": [], "verdicts": {"acme/occ#1": "wait"}},
                },
                {
                    "events": [{"release": {"pr": "acme/lib#2"}}],
                    "expect": {
                        "actions": ["companion_rebuild acme/occ#1 [acme/lib#2@h1]"]
                    },
                },
            ],
        }
    )


@pytest.mark.unit
def test_guard_m_p6_no_suppress() -> None:
    """R8: a blocked outcome suppresses redispatch while its fingerprint is unchanged."""
    run_scenario(
        {
            "id": "M_P6_no_suppress",
            "world": {"prs": [_pr("acme/app#1", **RED, blockers={"gate-1": "held"})]},
            "ticks": [
                {"expect": {"actions": ["dispatch_worker acme/app#1 real_red"]}},
                {
                    "events": [
                        {
                            "worker_result": {
                                "pr": "acme/app#1",
                                "kind": "external_blocker",
                                "blocker_kind": "production_gate",
                                "blocker_ref": "gate-1",
                            }
                        },
                        {"process_exit": {"pr": "acme/app#1"}},
                    ],
                    "expect": {
                        "actions": [],
                        "outcome": {"acme/app#1": "external_blocker/none"},
                    },
                },
                {"expect": {"actions": []}},
                {
                    "events": [
                        {
                            "ci": {
                                "pr": "acme/app#1",
                                "result": "red",
                                "red_checks": ["unit", "lint"],
                            }
                        }
                    ],
                    "expect": {"actions": []},
                },
                {
                    "events": [
                        {
                            "blocker": {
                                "pr": "acme/app#1",
                                "ref": "gate-1",
                                "state": "granted",
                            }
                        }
                    ],
                    "expect": {"actions": ["dispatch_worker acme/app#1 real_red"]},
                },
            ],
        }
    )


@pytest.mark.unit
def test_guard_m_p7_no_drain_gate() -> None:
    """P7: no dispatch in a draining repo; observe-only withholds every action."""
    run_scenario(
        {
            "id": "M_P7_no_drain_gate",
            "world": {"prs": [_pr("acme/app#1", **RED), _pr("acme/app#2", ci="green")]},
            "ticks": [
                {
                    "events": [{"drain": {"repo": "acme/app"}}],
                    "expect": {
                        "actions": ["observe_only acme/app"],
                        "observed": ["merge acme/app#2 h1"],
                    },
                },
                {"expect": {"actions": [], "observed": ["merge acme/app#2 h1"]}},
            ],
            "end": {"dispatches": {}, "pr_state": {"acme/app#2": "open"}},
        }
    )


@pytest.mark.unit
def test_guard_m_p8_release_on_deadline() -> None:
    """R7: a passed deadline starts the kill; it never releases the lease by itself."""
    run_scenario(
        {
            "id": "M_P8_release_on_deadline",
            "world": {"prs": [_pr("acme/app#1", **RED)]},
            "ticks": [
                {"expect": {"actions": ["dispatch_worker acme/app#1 real_red"]}},
                {
                    "events": [
                        {"clock": {"seconds": 3600}},
                        {"kill_fails": {"pr": "acme/app#1"}},
                    ],
                    "expect": {"actions": ["kill_worker acme/app#1"], "lease_count": 1},
                },
            ],
        }
    )


@pytest.mark.unit
def test_guard_m_p8_release_on_result() -> None:
    """R7: recording a result counts the outcome and releases nothing."""
    run_scenario(
        {
            "id": "M_P8_release_on_result",
            "world": {"prs": [_pr("acme/app#1", **RED)]},
            "ticks": [
                {"expect": {"actions": ["dispatch_worker acme/app#1 real_red"]}},
                {
                    "events": [
                        {"worker_result": {"pr": "acme/app#1", "kind": "waiting_ci"}}
                    ],
                    "expect": {
                        "actions": [],
                        "recorded": ["acme/app#1 invalid waiting_ci"],
                        "lease_count": 1,
                    },
                },
            ],
            "end": {"dispatches": {"acme/app#1": 1}},
        }
    )


@pytest.mark.unit
def test_guard_m_p9_producer_not_idem() -> None:
    """R2: a redelivery after a crash carries the same idempotency key K."""
    world = run_scenario(_fixture("S20_rebuild_crash_after_delivery.yaml"))
    keys = [
        action.rebuild_key
        for decision in world.decisions
        for action in decision.actions
        if action.kind is EnumLandingActionKind.COMPANION_REBUILD
    ]
    assert len(keys) == 2
    assert keys[0] == keys[1]


@pytest.mark.unit
def test_guard_m_r1_ancestry() -> None:
    """R1: a force-with-lease rebase under the lease verifies without ancestry (S16)."""
    run_scenario(_fixture("S16_fix_submitted_rebase_force_with_lease.yaml"))


@pytest.mark.unit
def test_guard_m_r1_headonly() -> None:
    """R1: a result naming the live head is refused when no lease authorized that head."""
    run_scenario(
        {
            "id": "M_R1_headonly",
            "world": {"prs": [_pr("acme/app#1", **RED)]},
            "ticks": [
                {"expect": {"actions": ["dispatch_worker acme/app#1 real_red"]}},
                {
                    "events": [
                        {"worker_push": {"pr": "acme/app#1"}},
                        {"foreign_rewrite": {"pr": "acme/app#1"}},
                        {
                            "worker_result": {
                                "pr": "acme/app#1",
                                "kind": "fix_submitted",
                            }
                        },
                    ],
                    "expect": {
                        "recorded": ["acme/app#1 invalid unauthorized_rewrite"],
                        "violations": 1,
                    },
                },
            ],
        }
    )


@pytest.mark.unit
def test_guard_m_r1_no_pin() -> None:
    """R1/R7: under a live lease, merge only the verified head, never a lingering push."""
    run_scenario(
        {
            "id": "M_R1_no_pin",
            "world": {"prs": [_pr("acme/app#1", **RED)]},
            "ticks": [
                {"expect": {"actions": ["dispatch_worker acme/app#1 real_red"]}},
                {
                    "events": [
                        {"worker_push": {"pr": "acme/app#1"}},
                        {
                            "worker_result": {
                                "pr": "acme/app#1",
                                "kind": "fix_submitted",
                            }
                        },
                    ],
                    "expect": {"actions": [], "awaiting": {"acme/app#1": "h2"}},
                },
                {
                    "events": [
                        {"worker_push": {"pr": "acme/app#1"}},
                        {"ci": {"pr": "acme/app#1", "result": "green"}},
                    ],
                    "expect": {"actions": ["kill_worker acme/app#1"]},
                },
                {
                    "expect": {
                        "actions": ["merge acme/app#1 h3"],
                        "lease": {"acme/app#1": None},
                    }
                },
            ],
        }
    )


@pytest.mark.unit
def test_guard_m_l2_suppress_any() -> None:
    """L2: a timed-out or invalid result is recovery, not suppression (S9, S10)."""
    run_scenario(_fixture("S09_recovery_after_timeout.yaml"))
    run_scenario(_fixture("S10_recovery_after_violation.yaml"))


@pytest.mark.unit
def test_guard_m_l4_no_redeliver() -> None:
    """R2 write-ahead: a pending, undelivered record is delivered by a later tick (S19)."""
    run_scenario(_fixture("S19_rebuild_crash_before_delivery.yaml"))


@pytest.mark.unit
def test_guard_m_l4_no_retry() -> None:
    """R2: a failed request is retried with the same key after its backoff (S18)."""
    run_scenario(_fixture("S18_rebuild_producer_failure.yaml"))


@pytest.mark.unit
def test_guard_m_l4_seen_open_only() -> None:
    """Model finding LC-F1: a rebuild succeeds when a companion carrying K exists in any state."""
    run_scenario(
        {
            "id": "M_L4_seen_open_only",
            "world": {
                "prs": [_pr("acme/app#1"), _pr("acme/lib#2")],
                "companions": [
                    {"pr": "acme/occ#1", "members": ["acme/app#1@h1", "acme/lib#2@h1"]}
                ],
            },
            "ticks": [
                {
                    "events": [{"member_push": {"pr": "acme/app#1"}}],
                    "expect": {
                        "actions": [
                            "companion_rebuild acme/occ#1 [acme/app#1@h2,acme/lib#2@h1]"
                        ]
                    },
                },
                {
                    "events": [
                        {"companion_merged_elsewhere": {"companion": "acme/occ#101"}}
                    ],
                    "expect": {
                        "actions": ["companion_close acme/occ#1 by acme/occ#101"],
                        "rebuilds": ["[acme/app#1@h2,acme/lib#2@h1] succeeded 1"],
                    },
                },
            ],
        }
    )


@pytest.mark.unit
def test_guard_m_lcover_no_cover() -> None:
    """R2: an uncovered member that is eligible again gets its own request (S15)."""
    run_scenario(_fixture("S15_batch_member_newly_held_same_head.yaml"))


# --------------------------------------------------------------------------
# Rules the model abstracts away.


@pytest.mark.unit
def test_rule_r5_one_rerun_per_head_then_fix_worker() -> None:
    run_scenario(
        {
            "id": "R5",
            "world": {
                "prs": [
                    _pr("acme/app#1", ci="red", red_class="cascade"),
                    _pr("acme/app#2", ci="red", red_class="runner_saturation"),
                ]
            },
            "ticks": [
                {"expect": {"actions": ["rerun acme/app#1", "rerun acme/app#2"]}},
                {"expect": {"actions": []}},
                {
                    "events": [
                        {
                            "ci": {
                                "pr": "acme/app#1",
                                "result": "red",
                                "red_class": "cascade",
                            }
                        },
                        {
                            "ci": {
                                "pr": "acme/app#2",
                                "result": "red",
                                "red_class": "runner_saturation",
                            }
                        },
                    ],
                    "expect": {
                        "actions": [
                            "dispatch_worker acme/app#1 cascade_red",
                            "dispatch_worker acme/app#2 real_red",
                        ]
                    },
                },
            ],
        }
    )


@pytest.mark.unit
def test_rule_d2_only_all_green_merges() -> None:
    run_scenario(
        {
            "id": "D2",
            "world": {
                "prs": [
                    _pr("acme/app#1", ci="pending"),
                    _pr("acme/app#2", ci="green", merge_state="conflicting"),
                    _pr("acme/app#3", ci="green"),
                ]
            },
            "ticks": [
                {
                    "expect": {
                        "actions": [
                            "merge acme/app#3 h1",
                            "dispatch_worker acme/app#2 conflict_mechanical",
                        ]
                    }
                }
            ],
        }
    )


@pytest.mark.unit
def test_rule_r3_chain_roots_only() -> None:
    run_scenario(
        {
            "id": "R3",
            "world": {
                "prs": [
                    _pr("acme/infra#4154", **RED),
                    _pr("acme/app#2343", **RED, parents=["acme/infra#4154"]),
                    _pr("acme/app#2371", ci="green", parents=["acme/infra#4154"]),
                ]
            },
            "ticks": [
                {
                    "expect": {
                        "actions": ["dispatch_worker acme/infra#4154 real_red"],
                        "recorded": ["acme/app#2343 blocked_on none"],
                        "outcome": {"acme/app#2343": "blocked_on/none"},
                    }
                },
                {"expect": {"actions": [], "recorded": []}},
                {
                    "events": [{"member_merged_elsewhere": {"pr": "acme/infra#4154"}}],
                    "expect": {
                        "actions": [
                            "dispatch_worker acme/app#2343 real_red",
                            "merge acme/app#2371 h1",
                        ]
                    },
                },
            ],
            "end": {"dispatches": {"acme/infra#4154": 1, "acme/app#2343": 1}},
        }
    )


@pytest.mark.unit
def test_rule_r4_priority_under_one_slot() -> None:
    run_scenario(
        {
            "id": "R4",
            "world": {
                "policy": {"max_workers": 1},
                "prs": [
                    _pr("acme/app#1", **RED),
                    _pr("acme/app#2", **RED, process_fix=True),
                    _pr("acme/app#3", **RED),
                    _pr("acme/app#4", ci="pending", parents=["acme/app#3"]),
                    _pr("acme/app#5", ci="pending", parents=["acme/app#3"]),
                ],
            },
            "ticks": [{"expect": {"actions": ["dispatch_worker acme/app#3 real_red"]}}],
        }
    )
    run_scenario(
        {
            "id": "R4b",
            "world": {
                "policy": {"max_workers": 1},
                "prs": [
                    _pr("acme/app#1", **RED),
                    _pr("acme/app#2", **RED, process_fix=True),
                ],
            },
            "ticks": [{"expect": {"actions": ["dispatch_worker acme/app#2 real_red"]}}],
        }
    )
    run_scenario(
        {
            "id": "R4c",
            "world": {
                "policy": {"max_workers": 1},
                "prs": [_pr("acme/app#1", **RED), _pr("acme/app#2", **RED)],
            },
            "ticks": [{"expect": {"actions": ["dispatch_worker acme/app#1 real_red"]}}],
        }
    )


@pytest.mark.unit
def test_rule_d5_pool_load_and_engine_fallback() -> None:
    run_scenario(
        {
            "id": "D5-load",
            "world": {"load1": 25.0, "prs": [_pr("acme/app#1", **RED)]},
            "ticks": [
                {"expect": {"actions": []}},
                {
                    "events": [{"load": {"load1": 3.0}}],
                    "expect": {"actions": ["dispatch_worker acme/app#1 real_red"]},
                },
            ],
        }
    )
    run_scenario(
        {
            "id": "D5-codex",
            "world": {
                "available_engines": ["codex_high"],
                "prs": [_pr("acme/app#1", **RED)],
            },
            "ticks": [
                {
                    "expect": {
                        "actions": ["dispatch_worker acme/app#1 real_red"],
                        "engine": {"acme/app#1": "codex_high"},
                    }
                }
            ],
        }
    )
    run_scenario(
        {
            "id": "D5-none",
            "world": {"available_engines": [], "prs": [_pr("acme/app#1", **RED)]},
            "ticks": [{"expect": {"actions": []}}],
        }
    )


def _invalid_then_exit(kind: str = "waiting_ci") -> list[dict[str, Any]]:
    return [
        {"worker_result": {"pr": "acme/app#1", "kind": kind}},
        {"process_exit": {"pr": "acme/app#1"}},
    ]


@pytest.mark.unit
def test_rule_r1_escalation_ladder_parks_after_three() -> None:
    run_scenario(
        {
            "id": "R1-ladder",
            "world": {"prs": [_pr("acme/app#1", **RED)]},
            "ticks": [
                {"expect": {"engine": {"acme/app#1": "claude_sonnet"}}},
                {
                    "events": _invalid_then_exit(),
                    "expect": {"engine": {"acme/app#1": "claude_opus"}},
                },
                {
                    "events": _invalid_then_exit("report_only"),
                    "expect": {"engine": {"acme/app#1": "codex_high"}},
                },
                {
                    "events": _invalid_then_exit("blocked"),
                    "expect": {
                        "actions": [],
                        "recorded": ["acme/app#1 invalid bare_blocked"],
                        "degraded": ["escalation_exhausted acme/app#1"],
                    },
                },
                {
                    "expect": {
                        "actions": [],
                        "degraded": ["escalation_exhausted acme/app#1"],
                    }
                },
                {
                    "events": [
                        {"member_push": {"pr": "acme/app#1"}},
                        {
                            "ci": {
                                "pr": "acme/app#1",
                                "result": "red",
                                "red_checks": ["unit"],
                            }
                        },
                    ],
                    "expect": {
                        "actions": ["dispatch_worker acme/app#1 real_red"],
                        "engine": {"acme/app#1": "claude_sonnet"},
                        "degraded": [],
                    },
                },
            ],
        }
    )


@pytest.mark.unit
def test_rule_r1_failed_attempt_escalates_new_red_does_not() -> None:
    fix = [
        {"worker_push": {"pr": "acme/app#1"}},
        {"worker_result": {"pr": "acme/app#1", "kind": "fix_submitted"}},
        {"process_exit": {"pr": "acme/app#1"}},
    ]
    run_scenario(
        {
            "id": "R1-failed-attempt",
            "world": {"prs": [_pr("acme/app#1", **RED)]},
            "ticks": [
                {"expect": {"engine": {"acme/app#1": "claude_sonnet"}}},
                {
                    "events": fix,
                    "expect": {
                        "actions": [],
                        "recorded": ["acme/app#1 fix_submitted none"],
                    },
                },
                {
                    "events": [
                        {
                            "ci": {
                                "pr": "acme/app#1",
                                "result": "red",
                                "red_checks": ["unit"],
                            }
                        }
                    ],
                    "expect": {"engine": {"acme/app#1": "claude_opus"}},
                },
            ],
        }
    )
    run_scenario(
        {
            "id": "R1-new-red",
            "world": {"prs": [_pr("acme/app#1", **RED)]},
            "ticks": [
                {"expect": {"engine": {"acme/app#1": "claude_sonnet"}}},
                {"events": fix, "expect": {"actions": []}},
                {
                    "events": [
                        {
                            "ci": {
                                "pr": "acme/app#1",
                                "result": "red",
                                "red_checks": ["lint"],
                            }
                        }
                    ],
                    "expect": {"engine": {"acme/app#1": "claude_sonnet"}},
                },
            ],
        }
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("waiting_ci", "waiting_ci"),
        ("waiting_order", "waiting_order"),
        ("report_only", "report_only"),
        ("blocked", "bare_blocked"),
        ("unparseable", "unparseable"),
        ("merged", "unverified_claim"),
        ("armed", "unverified_claim"),
    ],
)
def test_rule_r1_invalid_results(kind: str, reason: str) -> None:
    run_scenario(
        {
            "id": f"R1-invalid-{kind}",
            "world": {"prs": [_pr("acme/app#1", **RED)]},
            "ticks": [
                {},
                {
                    "events": _invalid_then_exit(kind),
                    "expect": {
                        "recorded": [f"acme/app#1 invalid {reason}"],
                        "violations": 1,
                        "engine": {"acme/app#1": "claude_opus"},
                    },
                },
            ],
        }
    )


@pytest.mark.unit
def test_rule_conflict_briefs_escalate() -> None:
    run_scenario(
        {
            "id": "conflict",
            "world": {
                "prs": [_pr("acme/app#1", ci="green", merge_state="conflicting")]
            },
            "ticks": [
                {
                    "expect": {
                        "actions": ["dispatch_worker acme/app#1 conflict_mechanical"]
                    }
                },
                {
                    "events": _invalid_then_exit(),
                    "expect": {
                        "actions": ["dispatch_worker acme/app#1 conflict_substantive"]
                    },
                },
            ],
        }
    )


@pytest.mark.unit
def test_rule_update_branch_then_behind_brief() -> None:
    """A behind head gets update_branch once per head; a refused update gets the behind brief."""
    run_scenario(
        {
            "id": "behind-moves",
            "update_branch_result": "behind",
            "world": {"prs": [_pr("acme/app#1", ci="green", merge_state="behind")]},
            "ticks": [
                {"expect": {"actions": ["update_branch acme/app#1"]}},
                {"expect": {"actions": ["update_branch acme/app#1"]}},
            ],
        }
    )
    run_scenario(
        {
            "id": "behind-refused",
            "update_branch_result": "refused",
            "world": {"prs": [_pr("acme/app#1", ci="green", merge_state="behind")]},
            "ticks": [
                {"expect": {"actions": ["update_branch acme/app#1"]}},
                {"expect": {"actions": ["dispatch_worker acme/app#1 behind"]}},
            ],
        }
    )


@pytest.mark.unit
def test_rule_runtime_brief_and_companion_briefs() -> None:
    run_scenario(
        {
            "id": "runtime",
            "world": {"prs": [_pr("acme/app#1", **RED, runtime=True)]},
            "ticks": [{"expect": {"actions": ["dispatch_worker acme/app#1 runtime"]}}],
        }
    )
    run_scenario(
        {
            "id": "companion-red",
            "world": {
                "prs": [_pr("acme/app#1")],
                "companions": [
                    {"pr": "acme/occ#1", "members": ["acme/app#1@h1"], "ci": "red"}
                ],
            },
            "ticks": [
                {
                    "expect": {
                        "actions": ["dispatch_worker acme/occ#1 companion_red"],
                        "verdicts": {"acme/occ#1": "companion_red"},
                    }
                }
            ],
        }
    )
    run_scenario(
        {
            "id": "companion-orphan",
            "close_fails": True,
            "world": {
                "prs": [_pr("acme/app#1")],
                "companions": [{"pr": "acme/occ#1", "members": ["acme/app#1@h1"]}],
            },
            "ticks": [
                {
                    "events": [{"member_close": {"pr": "acme/app#1"}}],
                    "expect": {"actions": ["companion_close acme/occ#1"]},
                },
                {
                    "expect": {
                        "actions": ["dispatch_worker acme/occ#1 companion_orphan"]
                    }
                },
            ],
        }
    )


@pytest.mark.unit
def test_rule_r1_armed_verified_against_auto_merge_head() -> None:
    spec: dict[str, Any] = {
        "id": "armed",
        "world": {"prs": [_pr("acme/app#1", **RED)]},
        "ticks": [{}],
    }
    world = run_scenario(spec)
    world.prs["acme/app#1"].auto_merge_head = 1
    world.apply_event({"worker_result": {"pr": "acme/app#1", "kind": "armed"}})
    decision = world.step({})
    assert [
        f"{r.outcome.value} {r.reason.value}" for r in decision.recorded_outcomes
    ] == ["armed none"]


@pytest.mark.unit
def test_rule_unknown_probe_fails_closed() -> None:
    """A lease with no process-table probe is treated as alive: never released on UNKNOWN."""
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    head = sha("acme/app#1", 1)
    lease = ModelLandingLease(
        pr="acme/app#1",
        lease_id=1,
        brief_class=EnumLandingBriefClass.REAL_RED,
        engine=EnumLandingEngine.CLAUDE_SONNET,
        dispatched_at=now,
        deadline_at=now,
        dispatch_head=head,
        seen_heads=(head,),
        last_seen_head=head,
    )
    facts = ModelLandingFacts(
        tick=2,
        observed_at=now,
        state=ModelLandingControllerState(
            last_tick=1, next_lease_id=2, leases=(lease,)
        ),
        prs=(
            ModelLandingPrFacts(
                pr="acme/app#1",
                head_sha=head,
                state="open",
                ci="red",
                red_class="product",
                created_at=now,
            ),
        ),
    )
    decision = decide_landing(facts)
    assert [le.lease_id for le in decision.next_state.leases] == [1]
    assert [a.kind for a in decision.actions] == [EnumLandingActionKind.KILL_WORKER]
