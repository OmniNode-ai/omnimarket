# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (OMN-23866): the h202-shaped lane package cache, 670 revisions, keep 3 days.

The reading is a synthetic fixture shaped like the one recorded on h202 (670 revisions over 40 packages); the
recording itself is not in this repository. Every revision's age and holder is a fixed function of its index, so the
expected eviction set is computed independently of the decision.
"""

from __future__ import annotations

import copy

import pytest

from omnimarket.models.model_host_disk_decision import (
    EnumHostDiskReason,
    ModelHostDiskPolicy,
)
from omnimarket.nodes.node_host_disk_decision_compute.handlers.handler_host_disk_decision import (
    decide_host_disk,
)

from .decision_inputs import (
    GIB,
    class_reading,
    example_policy_document,
    group,
    host_reading,
    request,
)

pytestmark = pytest.mark.unit

PACKAGES = 40
REVISIONS = 670


def _fixture() -> tuple[list, set[tuple[str, str]], set[tuple[str, str]]]:
    """Return the groups, the expected eviction set and the newest revision of each package."""
    groups = []
    for i in range(REVISIONS):
        package = f"pkg{i % PACKAGES:02d}"
        revision = f"rev{i:04d}"
        # age grows with the index: later indexes are older; some are young (< 3 days)
        age_days = (i % 17) * 0.5 + (i // PACKAGES) * 0.37
        held = 1 if i % 23 == 0 else 0
        groups.append(
            group(package, revision, age_days=age_days, bytes_=GIB // 4, held=held)
        )
    newest: dict[str, tuple[float, str]] = {}
    for g in groups:
        age = g.newest_use_age_seconds or 0
        if g.package not in newest or (age, g.revision) < newest[g.package]:
            newest[g.package] = (age, g.revision)
    keep_newest = {(p, rev) for p, (_, rev) in newest.items()}
    window = 3 * 86400
    expected = {
        (g.package, g.revision)
        for g in groups
        if (g.newest_use_age_seconds or 0) > window
        and g.held_copies == 0
        and (g.package, g.revision) not in keep_newest
    }
    return groups, expected, keep_newest


def _policy() -> ModelHostDiskPolicy:
    document = copy.deepcopy(example_policy_document())
    document["defaults"]["classes"]["uv_cache"] = {"keep_days": 3}
    return ModelHostDiskPolicy.model_validate(document)


def test_the_h202_package_cache_evicts_exactly_the_unheld_revisions_past_three_days() -> (
    None
):
    groups, expected, keep_newest = _fixture()
    assert len(groups) == REVISIONS
    assert expected  # the case is not vacuous
    assert len(expected) < REVISIONS
    reading = host_reading(
        "example-host-a", classes=(class_reading("uv_cache", tuple(groups)),)
    )
    decision = decide_host_disk(
        request(reading, host_reading("example-host-b"), policy=_policy())
    )
    [command] = decision.commands
    evicted = {(t.package, t.revision) for t in command.targets}
    assert evicted == expected
    assert command.reason is EnumHostDiskReason.AGE_PAST_KEEP_DAYS
    assert evicted.isdisjoint(keep_newest), "the newest revision of every package stays"
    held = {(g.package, g.revision) for g in groups if g.held_copies}
    assert evicted.isdisjoint(held), "a revision a live lane holds stays"
    assert {p for p, _ in keep_newest} == {f"pkg{i:02d}" for i in range(PACKAGES)}
