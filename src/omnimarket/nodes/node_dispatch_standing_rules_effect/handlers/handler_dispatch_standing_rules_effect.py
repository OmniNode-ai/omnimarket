# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read facts once, evaluate one snapshot, and persist admission audit rows."""

from datetime import UTC, datetime

from omnimarket.handlers.container.container_overlay_dependencies import (
    declared_handler_dependencies,
    overlay_documents,
    require_dependencies,
)
from omnimarket.models.standing_rules.model_standing_rules import (
    ModelStandingRuleFact,
    ModelStandingRulesAudit,
    ModelStandingRulesRegistry,
    ModelStandingRulesRequest,
    ModelStandingRulesResult,
    ModelStandingRulesSnapshot,
)
from omnimarket.nodes.node_dispatch_standing_rules_compute import (
    HandlerDispatchStandingRules,
)
from omnimarket.protocols.protocol_overlay_document import ProtocolOverlayDocument
from omnimarket.protocols.protocol_standing_rules_io import ProtocolStandingRulesIO

PACKAGE = "omnimarket.nodes.node_dispatch_standing_rules_effect"
IO_PROTOCOL = "omnimarket.protocols.protocol_standing_rules_io.ProtocolStandingRulesIO"


class HandlerDispatchStandingRulesEffect:
    """All entry kinds use this handler; dependencies come from the lane overlay."""

    def __init__(
        self,
        documents: ProtocolOverlayDocument | None = None,
        io: ProtocolStandingRulesIO | None = None,
    ) -> None:
        self._documents = documents
        self._io = io

    def handle(self, request: ModelStandingRulesRequest) -> ModelStandingRulesResult:
        now = request.now or datetime.now(UTC)
        registry: ModelStandingRulesRegistry | None = None
        try:
            if self._documents is None or self._io is None:
                deps = require_dependencies(declared_handler_dependencies(PACKAGE))
                self._documents = overlay_documents(deps)
                io = deps[IO_PROTOCOL]
                if not isinstance(io, ProtocolStandingRulesIO):
                    raise TypeError(
                        "standing_rules IO binding does not satisfy its protocol"
                    )
                self._io = io
            raw = self._documents.read("standing_rules")
            registry = ModelStandingRulesRegistry.model_validate(raw)
            ledger = self._io.read(registry.ledger.path)
            rulings = []
            for rule in registry.rules:
                source = rule.source_ruling
                for line in ledger.splitlines():
                    cells = [cell.strip() for cell in line.split("|")]
                    if len(cells) < 3 or cells[1] not in {"RULING", "OPERATOR-CONSENT"}:
                        continue
                    try:
                        stamp = datetime.fromisoformat(cells[0].replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if stamp == source.stamp and f"lane={source.lane}" in cells[2:]:
                        rulings.append(source)
                        break
            facts = {}
            for name, binding in registry.facts.items():
                try:
                    facts[name] = ModelStandingRuleFact.model_validate_json(
                        self._io.read(binding.path)
                    )
                except (OSError, ValueError, UnicodeError):
                    # Absence stays absence; the compute node decides if the request needs it.
                    continue
            snapshot = ModelStandingRulesSnapshot(
                dispatch=request.dispatch,
                now=now,
                registry=registry,
                rulings=tuple(rulings),
                facts=facts,
            )
            result = HandlerDispatchStandingRules().handle(snapshot)
            for rule_id in result.lapsed:
                self._io.append_once(
                    registry.audit_path,
                    f"LAPSED:{rule_id}",
                    ModelStandingRulesAudit(
                        at=now,
                        kind="LAPSED",
                        rule=rule_id,
                        request_id=request.dispatch.request_id,
                    ),
                )
            if result.verdict in {"refuse", "UNREAD"}:
                kind = "REFUSED" if result.verdict == "refuse" else "UNREAD"
                self._io.append_once(
                    registry.audit_path,
                    f"{kind}:{request.dispatch.request_id}:{result.rule}",
                    ModelStandingRulesAudit(
                        at=now,
                        kind=kind,
                        rule=result.rule,
                        request_id=request.dispatch.request_id,
                        fix=result.fix,
                    ),
                )
            return result
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
            read = (
                registry.ledger.read
                if registry
                else "read standing_rules overlay and its dependency bindings"
            )
            return ModelStandingRulesResult(
                request_id=request.dispatch.request_id,
                verdict="UNREAD",
                fix=f"UNREAD standing_rules ({type(exc).__name__}); FIX: {read}",
                terminal_failure_cause="unread",
            )
