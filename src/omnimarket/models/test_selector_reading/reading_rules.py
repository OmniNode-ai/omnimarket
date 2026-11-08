# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure rules of the test selector reading. No clock read, no I/O.

Activation of one repository's selector needs three things to read true: the workflow
file references the flag, the repository's variable holds ``true``, and branch protection
requires the selector's context. A source that reads false makes the repository DISABLED;
otherwise a source that could not be read makes it UNREADABLE.

Shadow misses: for each comparison on one head (selected head equals full-suite head), a
test the full suite failed that the narrowed run did not fail is a miss. A comparison on
two different heads proves nothing and is counted apart as unmatched. A DISABLED selector
adds one miss, so a repository whose selector is off never reads zero.
"""

from __future__ import annotations

import re

from omnimarket.models.test_selector_reading.model_test_selector_reading import (
    SELECTOR_FLAG_NAME,
    EnumSelectorActivation,
    EnumSelectorReadingState,
    ModelSelectorRepoObservation,
    ModelSelectorRepoReading,
    ModelTestSelectorReading,
    ModelTestSelectorReadingRequest,
)

_STATE = EnumSelectorReadingState
_FLAG = re.compile(rf"vars\.{SELECTOR_FLAG_NAME}\b")


def _missed(obs: ModelSelectorRepoObservation) -> tuple[int, int, tuple[str, ...]]:
    matched = unmatched = 0
    missed: list[str] = []
    for c in obs.comparisons:
        if c.selected_head_sha != c.full_suite_head_sha:
            unmatched += 1
            continue
        matched += 1
        seen = set(c.selected_failures)
        missed.extend(f"{c.pr}::{t}" for t in c.full_suite_failures if t not in seen)
    return matched, unmatched, tuple(missed)


def read_repo(obs: ModelSelectorRepoObservation) -> ModelSelectorRepoReading:
    """One repository's reading from its three sources and its shadow comparisons."""
    references: bool | None = (
        None if obs.workflow_text is None else bool(_FLAG.search(obs.workflow_text))
    )
    enabled: bool | None = (
        (obs.flag_value or "").strip().lower() == "true" if obs.flag_readable else None
    )
    required: bool | None = (
        None
        if obs.required_contexts is None
        else any(c in obs.required_contexts for c in obs.selector_contexts)
    )
    sources = {
        f"workflow does not reference {SELECTOR_FLAG_NAME}": references,
        f"{SELECTOR_FLAG_NAME} is not true": enabled,
        "branch protection does not require the selector": required,
    }
    off = [why for why, ok in sources.items() if ok is False]
    unread = [
        name
        for name, ok in (
            ("workflow", references),
            ("variable", enabled),
            ("branch protection", required),
        )
        if ok is None
    ]
    matched, unmatched, missed = _missed(obs)
    if off:
        activation = EnumSelectorActivation.DISABLED
    elif unread:
        activation = EnumSelectorActivation.UNREADABLE
    else:
        activation = EnumSelectorActivation.ACTIVE
    misses = len(missed) + (1 if activation is EnumSelectorActivation.DISABLED else 0)
    if activation is EnumSelectorActivation.DISABLED:
        state, reason = _STATE.NOT_WORKING, "; ".join(off)
    elif activation is EnumSelectorActivation.UNREADABLE:
        state, reason = _STATE.UNKNOWN, "unreadable: " + ", ".join(unread)
    elif missed:
        state = _STATE.NOT_WORKING
        reason = f"{len(missed)} failure(s) the full suite found were not found by the selector"
    else:
        state, reason = _STATE.WORKING, "active with no shadow miss"
    return ModelSelectorRepoReading(
        repo=obs.repo,
        state=state,
        activation=activation,
        workflow_references_flag=references,
        flag_enabled=enabled,
        protection_requires_selector=required,
        matched_comparisons=matched,
        unmatched_comparisons=unmatched,
        missed_failures=missed,
        shadow_misses=misses,
        reason=reason,
    )


def read_selector(request: ModelTestSelectorReadingRequest) -> ModelTestSelectorReading:
    """Read every expected repository; a repository with no observation reads UNKNOWN."""
    by_repo = {o.repo: o for o in request.observations}
    readings = tuple(
        read_repo(by_repo[r])
        if r in by_repo
        else read_repo(ModelSelectorRepoObservation(repo=r))
        for r in request.expected_repos
    )
    misses = sum(r.shadow_misses for r in readings)
    states = {r.state for r in readings}
    if _STATE.NOT_WORKING in states:
        state = _STATE.NOT_WORKING
    elif _STATE.UNKNOWN in states or not readings:
        state = _STATE.UNKNOWN
    else:
        state = _STATE.WORKING
    bad = [f"{r.repo}: {r.reason}" for r in readings if r.state is not _STATE.WORKING]
    reason = (
        "; ".join(bad)
        if bad
        else f"{len(readings)} repositories active, no shadow miss"
    )
    return ModelTestSelectorReading(
        state=state,
        shadow_misses=misses,
        read_at=request.now,
        repos=readings,
        reason=reason,
    )
