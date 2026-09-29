# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic watcher observations, without PR bodies."""

from omnimarket.events.pr_state import (
    EnumPrState,
    ModelPrStateObservedEvent,
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
