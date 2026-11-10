# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20172: the ladder's caps come from the task-class contract and nowhere else."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_shrink_retry_compute.handlers import (
    handler_shrink_retry_caps,
)

pytestmark = pytest.mark.unit


def test_a_class_that_declares_the_caps_resolves_them() -> None:
    caps = handler_shrink_retry_caps.resolve_task_class_shrink_caps("document")
    assert caps is not None
    assert (
        caps.max_config_swaps,
        caps.max_split_depth,
        caps.unit_attempts_per_unit,
    ) == (
        1,
        2,
        2,
    )
    assert "{source}" in caps.unit_prompt


def test_error_chain_an_unknown_class_declares_no_caps() -> None:
    assert (
        handler_shrink_retry_caps.resolve_task_class_shrink_caps("no_such_class_zz")
        is None
    )


def test_error_chain_a_class_without_a_source_slot_declares_no_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = {
        "escalation_policy": {
            "max_config_swaps": 1,
            "max_split_depth": 2,
            "unit_attempts_per_unit": 2,
            "shrink_unit_prompt": "no slot here",
        }
    }
    monkeypatch.setattr(
        handler_shrink_retry_caps, "_task_class_entry", lambda _c, _t: entry
    )
    assert handler_shrink_retry_caps.resolve_task_class_shrink_caps("document") is None
