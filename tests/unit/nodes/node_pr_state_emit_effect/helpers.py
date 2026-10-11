# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic watcher observations, without PR bodies."""

from omnimarket.events.pr_state import (
    EnumPrState,
    ModelPrStateObservedEvent,
    ModelPrStateObservedEventV2,
)


def event(**changes: object) -> ModelPrStateObservedEvent:
    fields: dict[str, object] = {
        "repo": "OmniNode-ai/omnimarket",
        "pr_number": 3050,
        "state": EnumPrState.OPEN,
        "head_sha": "a" * 40,
        "base": "dev",
        "head_ref": "feature/pr-state",
        "title": "PR state",
        "draft": False,
        "author": "developer",
        "author_is_bot": False,
        "labels": ("ready", "tested"),
        "armed": False,
        "queued": False,
        "watcher_class": "green-unarmed",
        "ci_verdict": "GREEN",
        "red_contexts": (),
        "pending_contexts": (),
        "ci_read_at": "2026-09-28T10:00:00Z",
        "merged_at": "",
        "observed_at": "2026-09-28T10:00:00Z",
    }
    fields.update(changes)
    return ModelPrStateObservedEvent.model_validate(fields)


def check_fact(check: str, **changes: object) -> dict[str, object]:
    fact: dict[str, object] = {
        "check": check,
        "conclusion": "failure",
        "run_id": 17000000001,
        "workflow": "CI",
        "completed_at": "2026-10-08T09:58:00Z",
    }
    fact.update(changes)
    return fact


def event_v2(**changes: object) -> ModelPrStateObservedEventV2:
    """A version 2 observation: a red head with per-check facts for every red context."""
    fields: dict[str, object] = {
        "ci_verdict": "RED",
        "red_contexts": ("CI Summary", "unit"),
        "schema_version": 2,
        "checks": (
            check_fact("CI Summary", run_id=17000000002),
            check_fact("unit", conclusion="timed_out"),
        ),
        "base_red_checks": (),
        "base_read": True,
    }
    fields.update(changes)
    return ModelPrStateObservedEventV2.model_validate(
        {**event().model_dump(exclude={"digest"}), **fields}
    )
