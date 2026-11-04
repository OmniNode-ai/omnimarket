# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabLanePriority -- the class a lane takes at placement (OMN-22124).

On 2026-11-02 a lane on omnibase_infra was refused PRIORITY-HOLDBACK with one pool slot free: the
remote-lane runner read only the lane's ticket for its class, so a repository the placement contract
lists under mvp-landing was charged to the lane as ``other``, and the slot held for mvp-landing was
held against an mvp-landing lane. A lane now takes, in order: the class its caller declared, the class
the contract lists its ticket (or its parent ticket) under, the class the contract lists its repository
under, else ``other``. The classes, repositories and tickets are the node contract's ``priority`` block.

The pool then admits or refuses the lane: no free slot refuses every class (the hosts refuse it); a
class at rank i leaves up to i free slots to the classes above it that have a unit waiting.
"""

from __future__ import annotations

from collections.abc import Collection
from importlib import resources
from typing import Literal

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_placement import (
    NODE_PACKAGE,
    HostTableError,
)
from omnimarket.nodes.node_lab_placement_compute.models.model_lab_lane_priority import (
    PRIORITY_OTHER,
    ModelLanePriorityRequest,
    ModelLanePriorityResult,
    ModelPriorityRules,
)


def load_priority_rules() -> ModelPriorityRules:
    """Read and check ``priority`` from this node's ``contract.yaml``."""
    text = resources.files(NODE_PACKAGE).joinpath("contract.yaml").read_text()
    block = (yaml.safe_load(text) or {}).get("priority")
    if not isinstance(block, dict):
        raise HostTableError("the lab placement contract declares no priority rules")
    try:
        return ModelPriorityRules.model_validate(block)
    except ValidationError as exc:
        raise HostTableError(
            f"the lab priority rules are invalid: {exc.errors()[0]['msg']}"
        ) from None


class HandlerLabLanePriority:
    """COMPUTE handler: the class a lane takes and whether the pool's free slots admit it."""

    def __init__(self, rules: ModelPriorityRules | None = None) -> None:
        self._rules = rules or load_priority_rules()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    @property
    def rules(self) -> ModelPriorityRules:
        return self._rules

    def priority_class_of_repo(self, repo: str | None) -> str:
        """The class the contract lists ``repo`` under, else ``other``."""
        return self._rules.class_of_repo(repo)

    def classify(self, request: ModelLanePriorityRequest) -> tuple[str, str]:
        """The lane's class and its source: declared, ticket, repository or default."""
        rules = (
            self._rules.with_repos(request.private_repos)
            if request.private_repos
            else self._rules
        )
        if request.declared in rules.classes:
            return str(request.declared), "declared"
        by_ticket = rules.class_of_ticket(request.ticket, request.parent)
        if by_ticket != PRIORITY_OTHER:
            return by_ticket, "ticket"
        by_repo = rules.class_of_repo(request.repo)
        if by_repo != PRIORITY_OTHER:
            return by_repo, "repository"
        return PRIORITY_OTHER, "default"

    def holdback_refusal(
        self, priority: str, free_slots: int, waiting: Collection[str]
    ) -> str | None:
        """Why a unit of class ``priority`` takes none of the pool's ``free_slots``, else None.

        A class leaves one free slot to each class above it that has a unit waiting. With no free slot the
        hosts refuse every class, so the holdback refuses only while 0 < free_slots <= its share. An
        unknown class is judged as ``other``."""
        classes = self._rules.classes
        cls = priority if priority in classes else PRIORITY_OTHER
        above = [c for c in classes[: classes.index(cls)] if c in waiting]
        if 0 < free_slots <= len(above):
            return (
                f"PRIORITY-HOLDBACK: the pool has {free_slots} free slot(s), held for "
                f"{', '.join(above)}; a {cls} unit waits for the next placement"
            )
        return None

    def handle(self, request: ModelLanePriorityRequest) -> ModelLanePriorityResult:
        cls, source = self.classify(request)
        refusal: str | None = None
        if request.free_slots == 0:
            refusal = "NO-FREE-SLOT: the pool has no free slot, whatever the class"
        elif request.free_slots is not None:
            refusal = self.holdback_refusal(cls, request.free_slots, request.waiting)
        return ModelLanePriorityResult(
            priority_class=cls, source=source, refusal=refusal
        )


__all__ = ["HandlerLabLanePriority", "load_priority_rules"]
