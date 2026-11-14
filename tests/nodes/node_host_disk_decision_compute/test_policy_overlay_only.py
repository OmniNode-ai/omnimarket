# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-23866): every keep-days value, cap, floor and per-host exception comes from the overlay."""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.model_host_disk_decision import (
    ModelHostDiskDecisionRequest,
    ModelHostDiskPolicy,
)
from omnimarket.nodes.node_host_disk_decision_compute.handlers import (
    handler_host_disk_decision,
)
from omnimarket.nodes.node_host_disk_decision_compute.handlers.handler_host_disk_decision import (
    decide_host_disk,
)

from .decision_inputs import (
    GIB,
    NOW,
    class_reading,
    example_policy,
    example_policy_document,
    group,
    host_reading,
    request,
)

pytestmark = pytest.mark.unit


def _groups():
    return (
        group("pkg", "r1", age_days=5),
        group("pkg", "r2", age_days=1),
    )


def test_the_handler_holds_no_policy_number() -> None:
    tree = ast.parse(Path(handler_host_disk_decision.__file__).read_text())
    numbers = {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, (int, float))
        and not isinstance(n.value, bool)
    }
    assert numbers <= {0, 1}, f"policy numbers in code: {sorted(numbers - {0, 1})}"


def test_a_keep_days_value_in_the_overlay_moves_the_decision() -> None:
    reading = host_reading(
        "example-host-a", classes=(class_reading("uv_cache", _groups()),)
    )
    other = host_reading("example-host-b")
    # example overlay: uv_cache keeps 7 days, so a 5 day old revision stays
    assert decide_host_disk(request(reading, other)).commands == ()
    document = copy.deepcopy(example_policy_document())
    document["defaults"]["classes"]["uv_cache"]["keep_days"] = 2
    policy = ModelHostDiskPolicy.model_validate(document)
    [command] = decide_host_disk(request(reading, other, policy=policy)).commands
    assert [t.revision for t in command.targets] == ["r1"]


def test_a_per_host_exception_wins_over_the_default() -> None:
    groups = (group("pkg", "r1", age_days=5), group("pkg", "r2", age_days=1))
    host_b = host_reading(
        "example-host-b", classes=(class_reading("uv_cache", groups),)
    )
    host_a = host_reading(
        "example-host-a", classes=(class_reading("uv_cache", groups),)
    )
    decision = decide_host_disk(request(host_a, host_b))
    # host b's own block keeps 3 days; the default keeps 7
    assert [c.host_name for c in decision.commands] == ["example-host-b"]


def test_a_per_host_floor_moves_the_alarm() -> None:
    free = 150 * GIB  # above the default floor (100 GiB), under host b's own (200 GiB)
    decision = decide_host_disk(
        request(
            host_reading("example-host-a", free_bytes=free),
            host_reading("example-host-b", free_bytes=free),
        )
    )
    assert [a.host_name for a in decision.alarms] == ["example-host-b"]


def test_a_policy_that_leaves_a_host_without_a_floor_is_refused() -> None:
    document = copy.deepcopy(example_policy_document())
    del document["defaults"]["floor_free_bytes"]
    with pytest.raises(ValidationError, match=r"example-host-a.*no free-space floor"):
        ModelHostDiskPolicy.model_validate(document)


def test_a_reading_for_a_host_the_policy_does_not_name_is_refused_without_a_default_floor() -> (
    None
):
    document = copy.deepcopy(example_policy_document())
    del document["defaults"]["floor_free_bytes"]
    document["hosts"] = {"example-host-a": {"floor_free_bytes": 100 * GIB}}
    policy = ModelHostDiskPolicy.model_validate(document)
    with pytest.raises(ValidationError, match=r"stranger.*no free-space floor"):
        ModelHostDiskDecisionRequest(
            now=NOW, policy=policy, readings=(host_reading("stranger"),)
        )


def test_the_example_overlay_is_a_valid_policy() -> None:
    assert example_policy().floor_for("example-host-b") == 200 * GIB
