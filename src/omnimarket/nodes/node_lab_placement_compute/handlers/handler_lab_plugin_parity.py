# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabPluginParity -- does an installed plugin equal its shipped revision (OMN-22124).

On 2026-11-02 a lane pinned to a lab host was refused PARITY-FAILED because the installed plugin
differed from its shipped revision by one added file, ``.orphaned_at``: a retirement stamp Claude Code
writes into a superseded plugin directory. It is not plugin content. The entries the contract's
``parity.ignored_top_level`` names, directly under the install path, are left out of the comparison;
the same name at any other depth is a file like any other.
"""

from __future__ import annotations

from importlib import resources
from typing import Literal

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_placement import (
    NODE_PACKAGE,
    HostTableError,
)
from omnimarket.nodes.node_lab_placement_compute.models.model_lab_lane_priority import (
    ModelParityRules,
    ModelPluginParityRequest,
    ModelPluginParityResult,
)


def load_parity_rules() -> ModelParityRules:
    """Read and check ``parity`` from this node's ``contract.yaml``."""
    text = resources.files(NODE_PACKAGE).joinpath("contract.yaml").read_text()
    block = (yaml.safe_load(text) or {}).get("parity")
    if not isinstance(block, dict):
        raise HostTableError("the lab placement contract declares no parity rules")
    try:
        return ModelParityRules.model_validate(block)
    except ValidationError as exc:
        raise HostTableError(
            f"the lab parity rules are invalid: {exc.errors()[0]['msg']}"
        ) from None


class HandlerLabPluginParity:
    """COMPUTE handler: compare an installed plugin tree with its shipped revision's."""

    def __init__(self, rules: ModelParityRules | None = None) -> None:
        self._rules = rules or load_parity_rules()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    @property
    def rules(self) -> ModelParityRules:
        return self._rules

    def is_runtime_marker(self, path: str) -> bool:
        """True for an entry directly under the install path the contract names (a marker file, or anything under a marker directory)."""
        top = path.split("/", 1)[0]
        return top in self._rules.ignored_top_level

    def handle(self, request: ModelPluginParityRequest) -> ModelPluginParityResult:
        installed = dict(request.installed)
        ignored = tuple(sorted(p for p in installed if self.is_runtime_marker(p)))
        kept = {p: b for p, b in installed.items() if not self.is_runtime_marker(p)}
        shipped = {
            p: b for p, b in request.shipped.items() if not self.is_runtime_marker(p)
        }
        added = tuple(sorted(set(kept) - set(shipped)))
        removed = tuple(sorted(set(shipped) - set(kept)))
        changed = tuple(
            sorted(p for p in set(kept) & set(shipped) if kept[p] != shipped[p])
        )
        state = "modified" if added or removed or changed else "ok"
        return ModelPluginParityResult(
            state=state, added=added, removed=removed, changed=changed, ignored=ignored
        )


__all__ = ["HandlerLabPluginParity", "load_parity_rules"]
