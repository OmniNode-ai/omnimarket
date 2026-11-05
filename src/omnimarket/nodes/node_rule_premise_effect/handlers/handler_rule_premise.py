# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Extract references, read their state, and ask the core validator before every send."""

import hashlib
import re

from omnimarket.handlers.container.container_overlay_dependencies import (
    declared_handler_dependencies,
    packaged_contract_value,
    require_dependencies,
)
from omnimarket.models.model_rule_premise import (
    ModelBatchPolicy,
    ModelBatchRequest,
    ModelBriefCheckRequest,
    ModelBriefCheckResult,
    ModelBriefPremise,
    ModelPremiseRule,
    ModelPremiseValidationRequest,
)
from omnimarket.protocols.protocol_rule_premise import (
    ProtocolBatchClaimStore,
    ProtocolPremiseReader,
    ProtocolPremiseValidator,
)

PACKAGE = "omnimarket.nodes.node_rule_premise_effect"
READER = "omnimarket.protocols.protocol_rule_premise.ProtocolPremiseReader"
VALIDATOR = "omnimarket.protocols.protocol_rule_premise.ProtocolPremiseValidator"
_PR = re.compile(r"(?<![\w/.-])(?P<repo>(?:[\w.-]+/)?[\w.-]+)#(?P<n>[1-9]\d*)\b")
_URL = re.compile(
    r"https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<n>[1-9]\d*)\b"
)
_TICKET = re.compile(r"\bOMN-[1-9]\d*\b")
_HEAD = re.compile(r"(?<![\w])[0-9a-f]{40}(?![\w])")
_NAMED_HEAD = re.compile(
    r"\b(?:head|sha|revision|ref)\s*[=: ]\s*(?P<head>[0-9a-f]{7,64})(?![\w])",
    re.IGNORECASE,
)


def extract_premises(brief: str, repository: str = "") -> tuple[ModelBriefPremise, ...]:
    """References are ordered by position and deduplicated, including GitHub PR URLs."""
    found: list[tuple[int, ModelBriefPremise]] = []
    for pattern in (_PR, _URL):
        for match in pattern.finditer(brief):
            repo = match["repo"]
            found.append(
                (
                    match.start(),
                    ModelBriefPremise(
                        kind="pr", reference=f"{repo}#{match['n']}", repository=repo
                    ),
                )
            )
    for match in _TICKET.finditer(brief):
        found.append(
            (match.start(), ModelBriefPremise(kind="ticket", reference=match[0]))
        )
    for match in _HEAD.finditer(brief):
        found.append(
            (
                match.start(),
                ModelBriefPremise(
                    kind="head", reference=match[0], repository=repository
                ),
            )
        )
    for match in _NAMED_HEAD.finditer(brief):
        found.append(
            (
                match.start("head"),
                ModelBriefPremise(
                    kind="head", reference=match["head"], repository=repository
                ),
            )
        )
    seen: set[tuple[str, str]] = set()
    result = []
    for _, premise in sorted(found, key=lambda item: item[0]):
        key = (premise.kind, premise.reference)
        if key not in seen:
            result.append(premise)
            seen.add(key)
    return tuple(result)


def brief_receipt(brief: str) -> str:
    return "brief-check=pass:" + hashlib.sha256(brief.encode("utf-8")).hexdigest()


def receipt_allows(
    brief: str, result: ModelBriefCheckResult, *, require_admission: bool = False
) -> bool:
    return (
        result.verdict == "pass"
        and result.receipt == brief_receipt(brief)
        and (not require_admission or (bool(result.slot) and result.attempt > 0))
    )


class HandlerRulePremise:
    def __init__(
        self,
        reader: ProtocolPremiseReader | None = None,
        validator: ProtocolPremiseValidator | None = None,
        batch_store: ProtocolBatchClaimStore | None = None,
        policy: ModelBatchPolicy | None = None,
    ) -> None:
        self._reader = reader
        self._validator = validator
        self._batch_store = batch_store
        self._policy = policy

    def handle(self, request: ModelBriefCheckRequest) -> ModelBriefCheckResult:
        premises = extract_premises(request.brief, request.repository)
        try:
            reader, validator = self._reader, self._validator
            if reader is None or validator is None:
                dependencies = require_dependencies(
                    declared_handler_dependencies(PACKAGE)
                )
                bound_reader = dependencies[READER]
                bound_validator = dependencies[VALIDATOR]
                if not isinstance(
                    bound_reader, ProtocolPremiseReader
                ) or not isinstance(bound_validator, ProtocolPremiseValidator):
                    raise TypeError(
                        "R-B reader or core validator binding has the wrong protocol"
                    )
                reader, validator = bound_reader, bound_validator
            reads = tuple(reader.read(premise, request.host) for premise in premises)
            if any(
                not read.readable or read.premise != premise
                for premise, read in zip(premises, reads, strict=True)
            ):
                return ModelBriefCheckResult(
                    verdict="UNREAD",
                    reason="premise live read unavailable",
                    premises=premises,
                )
            waiting = tuple(
                read
                for read in reads
                if (
                    read.premise.kind == "head"
                    or (read.premise.kind == "pr" and not read.merged)
                )
                and not (read.head and read.installed_revision and read.contained)
            )
            if waiting:
                return ModelBriefCheckResult(
                    verdict="wait",
                    reason="premise head is not installed on the target host",
                    premises=premises,
                    install_rows=tuple(
                        dict.fromkeys(
                            read.install_row for read in waiting if read.install_row
                        )
                    ),
                )
            checked = validator.check(
                ModelPremiseValidationRequest(
                    rule=ModelPremiseRule.model_validate(
                        packaged_contract_value(PACKAGE, "rule")
                    ),
                    request=request,
                    premises=premises,
                    reads=reads,
                )
            )
            if not receipt_allows(request.brief, checked):
                return (
                    checked.model_copy(update={"verdict": "refuse"})
                    if checked.verdict == "pass"
                    else checked
                )
            if not request.batch:
                return ModelBriefCheckResult(
                    verdict="refuse",
                    reason="batch identity missing; admission cannot reserve a probe slot",
                    premises=premises,
                )
            if request.batch:
                from .handler_batch_probe import HandlerBatchProbe

                admitted = HandlerBatchProbe(
                    store=self._batch_store, policy=self._policy, validator=validator
                ).handle(
                    ModelBatchRequest(
                        operation="admit",
                        batch=request.batch,
                        brief_id=request.brief_id
                        or hashlib.sha256(request.brief.encode("utf-8")).hexdigest(),
                        premise=request.premise
                        or hashlib.sha256(
                            "\n".join(p.model_dump_json() for p in premises).encode(
                                "utf-8"
                            )
                        ).hexdigest(),
                        now=request.now,
                        work_waiting=request.work_waiting,
                    )
                )
                if admitted.verdict != "admit":
                    return ModelBriefCheckResult(
                        verdict="wait", reason=admitted.reason, premises=premises
                    )
                checked = checked.model_copy(
                    update={"slot": admitted.slot, "attempt": admitted.attempt}
                )
            return checked.model_copy(update={"premises": premises})
        except (
            OSError,
            ValueError,
            TypeError,
            LookupError,
            RuntimeError,
            ImportError,
            AttributeError,
        ) as exc:
            return ModelBriefCheckResult(
                verdict="UNREAD", reason=f"R-B: {exc}", premises=premises
            )
