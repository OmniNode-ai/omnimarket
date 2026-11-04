# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the item fold and the turn's accepted CLAIM bindings through declared IO."""

import json
from datetime import datetime

from omnimarket.handlers.container.container_overlay_dependencies import (
    declared_handler_dependencies,
    overlay_documents,
    require_dependencies,
)
from omnimarket.models.standing_rules.model_rule_cause_idle import (
    ModelAcceptedItemStart,
    ModelProcessFixInput,
    ModelProcessFixReadRequest,
    ModelProcessFixReadResult,
)
from omnimarket.models.standing_rules.model_standing_rules import (
    ModelStandingRulesRegistry,
)
from omnimarket.protocols.protocol_overlay_document import ProtocolOverlayDocument
from omnimarket.protocols.protocol_standing_rules_io import ProtocolStandingRulesIO

PACKAGE = "omnimarket.nodes.node_dispatch_standing_rules_effect"
IO_PROTOCOL = "omnimarket.protocols.protocol_standing_rules_io.ProtocolStandingRulesIO"


class HandlerProcessFixRead:
    def __init__(
        self,
        documents: ProtocolOverlayDocument | None = None,
        io: ProtocolStandingRulesIO | None = None,
    ) -> None:
        self._documents = documents
        self._io = io

    def handle(self, request: ModelProcessFixReadRequest) -> ModelProcessFixReadResult:
        if self._documents is None or self._io is None:
            deps = require_dependencies(declared_handler_dependencies(PACKAGE))
            self._documents = overlay_documents(deps)
            io = deps[IO_PROTOCOL]
            if not isinstance(io, ProtocolStandingRulesIO):
                raise TypeError("standing rules IO protocol unbound")
            self._io = io
        registry = ModelStandingRulesRegistry.model_validate(
            self._documents.read("standing_rules")
        )
        rule = next(rule for rule in registry.rules if rule.check == "process_fill")
        ledger = self._io.read(registry.ledger.path)
        source_found = False
        starts = []
        for line in ledger.splitlines():
            cells = [cell.strip() for cell in line.split("|")]
            if len(cells) < 3:
                continue
            fields = dict(cell.split("=", 1) for cell in cells[2:] if "=" in cell)
            try:
                at = datetime.fromisoformat(cells[0].replace("Z", "+00:00"))
            except ValueError:
                continue
            if (
                cells[1] in {"RULING", "OPERATOR-CONSENT"}
                and at == rule.source_ruling.stamp
                and fields.get("lane") == rule.source_ruling.lane
            ):
                source_found = True
            if (
                request.turn_started_at is not None
                and at.tzinfo is not None
                and request.turn_started_at <= at <= request.now
                and cells[1] == "CLAIM"
                and fields.get("lane")
            ):
                starts.append(
                    ModelAcceptedItemStart(
                        lane=fields["lane"],
                        for_item=fields.get("for-item"),
                        ask=fields.get("ask"),
                    )
                )
        if not source_found:
            raise ValueError(f"UNREAD R-D source ruling; {registry.ledger.read}")
        raw = json.loads(self._io.read(rule.item_fold_path))
        source = ModelProcessFixInput.model_validate({"rule": rule, "items": raw})
        return ModelProcessFixReadResult(
            process_fixes=source, accepted_starts=tuple(starts)
        )
