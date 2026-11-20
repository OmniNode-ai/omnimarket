# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Assign selected changes to hosts and build their child workflow plans."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable

from omnimarket.nodes.node_prove_running_workflow.models import (
    ModelProveRunningChange,
    ModelProveRunningHostGroup,
    ModelProveRunningHostGroups,
    ModelProveRunningPlan,
)


def group_by_host(
    changes: Iterable[ModelProveRunningChange],
    plans_builder: Callable[[ModelProveRunningChange], ModelProveRunningPlan],
    hosts: tuple[str, ...],
    default_host: str,
) -> ModelProveRunningHostGroups:
    """Group by host name, preserving the original order of changes on each host."""
    patterns = tuple(
        (host, re.compile(rf"\b{re.escape(host)}\b", re.IGNORECASE)) for host in hosts
    )
    groups: dict[str, list[ModelProveRunningPlan]] = defaultdict(list)
    for change in changes:
        host = next(
            (host for host, pattern in patterns if pattern.search(change.target)),
            default_host,
        )
        groups[host].append(plans_builder(change.model_copy(update={"target": host})))
    return ModelProveRunningHostGroups(
        groups=tuple(
            ModelProveRunningHostGroup(host=host, plans=tuple(plans))
            for host, plans in sorted(groups.items())
        )
    )
