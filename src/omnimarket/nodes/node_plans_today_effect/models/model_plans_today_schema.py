# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The JSON schema of ``plans-today.json`` (draft 2020-12)."""

from __future__ import annotations

from typing import Any

PLANS_TODAY_SCHEMA_ID = "omni.plans-today/2"

PLANS_TODAY_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "omni.plans-today/2",
    "title": "plans-today output",
    "description": "The ranked plans of the day, written beside detail.md. validate() in "
    "handler_plans_today.py enforces this schema's required fields and enums.",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema", "generated_at", "mode", "scope", "sources", "plans", "msgs"],
    "properties": {
        "schema": {"const": "omni.plans-today/2"},
        "generated_at": {
            "type": "string",
            "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z$",
        },
        "sources": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "kb_internal_origin_main",
                "watcher_last_tick",
                "ledger_newest_row",
                "linear_snapshot",
                "goal_state_as_of",
                "pr_source",
                "pr_reads",
                "pr_read_cap",
                "team_identity",
                "linear_sidecar",
            ],
            "properties": {
                "kb_internal_origin_main": {
                    "type": "string",
                    "description": "sha of "
                    "origin/main "
                    "of the "
                    "plans "
                    "repository "
                    "that the "
                    "plans were "
                    "read at",
                },
                "watcher_last_tick": {
                    "type": "string",
                    "description": "last_tick of the "
                    "pr-watcher state "
                    "file; in member "
                    "mode a note that "
                    "it was not read",
                },
                "ledger_newest_row": {
                    "type": "string",
                    "description": "timestamp of the "
                    "newest ledger row "
                    "the reader "
                    "returned",
                },
                "linear_snapshot": {
                    "type": "string",
                    "description": "path of the Linear "
                    "snapshot, or a note "
                    "that none was used",
                },
                "goal_state_as_of": {
                    "type": "string",
                    "description": "state_as_of stamp "
                    "of beta/GOAL.md at "
                    "origin/main, or "
                    "absent",
                },
                "pr_source": {
                    "enum": ["pr-watcher", "gh-pr-view"],
                    "description": "where pull request state "
                    "came from: the watcher "
                    "file (operator) or one gh "
                    "pr view per scoped pull "
                    "request (member)",
                },
                "pr_reads": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "description": "gh pr view reads made; null for the watcher source",
                },
                "pr_read_cap": {
                    "type": ["integer", "null"],
                    "description": "the most reads member "
                    "mode makes before it "
                    "refuses; null for the "
                    "watcher source",
                },
                "team_identity": {
                    "type": "string",
                    "description": "the declaration the "
                    "role was resolved "
                    "from (path and "
                    "sha256) and the "
                    "privileges it gave",
                },
                "linear_sidecar": {
                    "type": "string",
                    "description": "path of the Linear "
                    "sidecar the subject "
                    "was read from",
                },
            },
        },
        "plans": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "path",
                    "title",
                    "verdict",
                    "rank",
                    "owner",
                    "hours_idle",
                    "tasks",
                    "next_action",
                    "next_action_kind",
                    "next_action_ticket",
                    "priority",
                    "priority_source",
                    "summary",
                    "summary_source",
                ],
                "properties": {
                    "path": {"type": "string"},
                    "title": {"type": "string"},
                    "date": {"type": ["string", "null"]},
                    "selected_by": {
                        "type": "array",
                        "items": {
                            "enum": [
                                "dated",
                                "in-progress-ticket",
                                "open-pr",
                                "member-scope",
                            ]
                        },
                    },
                    "verdict": {
                        "enum": ["STALLED", "IN-PROGRESS", "NOT-STARTED", "DONE"]
                    },
                    "verdict_reason": {"type": "string"},
                    "rank": {"type": "integer", "minimum": 1},
                    "owner": {
                        "type": "string",
                        "description": "the live lane(s) "
                        "holding an open CLAIM "
                        "on the plan's tickets "
                        "or PRs, or NONE "
                        "(optionally with the "
                        "stale lanes)",
                    },
                    "hours_idle": {
                        "type": ["number", "null"],
                        "description": "hours since the "
                        "newest ledger "
                        "row, PR update "
                        "or Linear update "
                        "for the plan's "
                        "tickets; null "
                        "when none is on "
                        "record",
                    },
                    "last_movement": {"type": ["string", "null"]},
                    "counts": {"type": "object"},
                    "tickets": {"type": "array", "items": {"type": "string"}},
                    "prs": {"type": "array", "items": {"type": "string"}},
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": [
                                "label",
                                "tickets",
                                "prs",
                                "state",
                                "owner",
                                "in_scope",
                            ],
                            "properties": {
                                "label": {"type": "string"},
                                "text": {"type": "string"},
                                "tickets": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "prs": {"type": "array", "items": {"type": "string"}},
                                "state": {
                                    "enum": [
                                        "merged",
                                        "open-green",
                                        "open-red",
                                        "not-started",
                                        "unread",
                                    ],
                                    "description": "unread: "
                                    "a "
                                    "task "
                                    "outside "
                                    "the "
                                    "member's "
                                    "scope, "
                                    "listed "
                                    "as "
                                    "'other "
                                    "member: "
                                    "state "
                                    "not "
                                    "read' "
                                    "and "
                                    "never "
                                    "dispatched",
                                },
                                "state_basis": {"type": "string"},
                                "owner": {"type": "string"},
                                "no_ticket": {"type": "boolean"},
                                "in_scope": {"type": "boolean"},
                                "unread_tickets": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                    "next_action": {"type": "string"},
                    "next_action_kind": {
                        "enum": [
                            "dispatch-fix",
                            "hand-to-drain",
                            "dispatch",
                            "land-or-close",
                            "mint-tickets",
                            "read-plan",
                            "wait",
                            "decide",
                            "none",
                        ],
                        "description": "what kind "
                        "of move "
                        "next_action "
                        "is; a "
                        "machine "
                        "reader "
                        "acts on "
                        "the kind "
                        "and the "
                        "ticket, "
                        "never on "
                        "the "
                        "sentence",
                    },
                    "next_action_ticket": {
                        "type": ["string", "null"],
                        "pattern": "^OMN-\\d+$",
                        "description": "the "
                        "ticket "
                        "the next "
                        "action "
                        "is about "
                        "(the "
                        "first "
                        "ticket "
                        "of the "
                        "task it "
                        "names, "
                        "else the "
                        "plan's "
                        "first "
                        "ticket); "
                        "null "
                        "when the "
                        "move "
                        "names no "
                        "ticket",
                    },
                    "priority": {
                        "enum": ["urgent", "high", "medium", "low", "unset"],
                        "description": "the plan's own "
                        "Priority header "
                        "line, else the "
                        "highest Linear "
                        "priority among its "
                        "tickets, else "
                        "unset",
                    },
                    "priority_source": {"enum": ["plan-header", "linear", "none"]},
                    "summary": {"type": "string"},
                    "summary_source": {
                        "enum": ["lab", "lab-inprocess", "glm", "template"]
                    },
                    "delegation_runs": {"type": "array", "items": {"type": "string"}},
                    "summary_rejections": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        "msgs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["plan", "action", "detail"],
                "properties": {
                    "plan": {"type": "string"},
                    "action": {"enum": ["sent", "skipped", "would-send", "failed"]},
                    "detail": {"type": "string"},
                },
            },
        },
        "mode": {
            "enum": ["operator", "member"],
            "description": "resolved through the team identity declaration on every "
            "run: operator only for an authenticated operator match",
        },
        "scope": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "member", "tickets", "assigned", "claimed"],
            "properties": {
                "kind": {
                    "enum": ["all", "member"],
                    "description": "all: every plan task is read "
                    "(the operator's mode); member: "
                    "only the member's tickets are "
                    "read and ranked",
                },
                "member": {
                    "type": ["string", "null"],
                    "description": "the member's team identity "
                    "entry id; null in the "
                    "operator's mode",
                },
                "tickets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "the member's scope: assigned "
                    "or named on an open CLAIM of "
                    "the member's",
                },
                "assigned": {"type": "array", "items": {"type": "string"}},
                "claimed": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}
