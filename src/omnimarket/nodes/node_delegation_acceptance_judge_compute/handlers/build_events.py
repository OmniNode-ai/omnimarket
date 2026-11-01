# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build judged-acceptance events from validated primary verdicts and the run's calibration. Pure.

One event per judged (non-probe) item. The result is all of the run's events or none: an item
with no verdict, no call, no delegated model key or tier, or an unsupported kind refuses the run.
No file, clock, model or bus is touched; the judge's identity and each call's identity are inputs.
"""

from __future__ import annotations

from collections import Counter

from omnimarket.models.delegation_acceptance_judge.model_acceptance_event_call import (
    ModelAcceptanceEventCall,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_event_judge import (
    ModelAcceptanceEventJudge,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_item import (
    ModelAcceptanceItem,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_verdict import (
    ModelAcceptanceVerdict,
)
from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
    ModelDelegationAcceptanceJudgedEvent,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.classify import (
    is_probe,
    kind_of,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_issue_code import (
    EnumAcceptanceIssueCode as Code,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_agreement import (
    ModelAcceptanceAgreement,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_issue import (
    ModelAcceptanceIssue,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_rubric import (
    ModelAcceptanceRubric,
)

_KINDS = ("task", "edit_loop_turn")


def _issue(code: Code, item_id: str, message: str) -> ModelAcceptanceIssue:
    return ModelAcceptanceIssue(code=code, item_id=item_id, message=message)


def build_events(
    items: tuple[ModelAcceptanceItem, ...],
    primary_verdicts: tuple[ModelAcceptanceVerdict, ...],
    agreement: ModelAcceptanceAgreement,
    calls: tuple[ModelAcceptanceEventCall, ...],
    judge: ModelAcceptanceEventJudge,
    rubric: ModelAcceptanceRubric,
) -> tuple[list[ModelDelegationAcceptanceJudgedEvent], list[ModelAcceptanceIssue]]:
    issues: list[ModelAcceptanceIssue] = []
    judged = [
        item
        for item in items
        if not is_probe(item.task_text, len(item.task_text), rubric)
    ]
    known = {item.item_id for item in judged}
    verdict_counts = Counter(v.item_id for v in primary_verdicts)
    call_counts = Counter(c.item_id for c in calls)
    verdicts = {v.item_id: v for v in primary_verdicts}
    by_call = {c.item_id: c for c in calls}
    if agreement.double_judged == 0:
        issues.append(
            _issue(
                Code.CALIBRATION_MISSING,
                "",
                "no item was judged by both judges, so the events have no calibration to carry",
            )
        )
    for item_id in sorted(set(verdict_counts) - known):
        issues.append(
            _issue(
                Code.UNKNOWN_ITEM,
                item_id,
                "a primary verdict names an item that is not a judged item of the run",
            )
        )
    for item_id in sorted(i for i, n in verdict_counts.items() if n > 1):
        issues.append(
            _issue(
                Code.DUPLICATE_ITEM,
                item_id,
                f"{verdict_counts[item_id]} primary verdicts for one item",
            )
        )
    for item_id in sorted(i for i, n in call_counts.items() if n > 1):
        issues.append(
            _issue(
                Code.DUPLICATE_EVENT_CALL,
                item_id,
                f"{call_counts[item_id]} calls recorded for one item",
            )
        )
    events: list[ModelDelegationAcceptanceJudgedEvent] = []
    for item in judged:
        verdict = verdicts.get(item.item_id)
        call = by_call.get(item.item_id)
        kind = kind_of(item.task_text, item.kind, rubric)
        problems = len(issues)
        if verdict is None:
            issues.append(
                _issue(
                    Code.PRIMARY_VERDICT_MISSING,
                    item.item_id,
                    "the primary judge has no verdict for this item",
                )
            )
        if call is None:
            issues.append(
                _issue(
                    Code.EVENT_CALL_MISSING,
                    item.item_id,
                    "no delegated call is recorded for this item",
                )
            )
        else:
            if not call.delegated_model_key:
                issues.append(
                    _issue(
                        Code.MODEL_KEY_MISSING,
                        item.item_id,
                        "the item has no delegated model key",
                    )
                )
            if not call.delegated_tier:
                issues.append(
                    _issue(
                        Code.TIER_MISSING,
                        item.item_id,
                        "the item has no delegated tier",
                    )
                )
        if kind not in _KINDS:
            issues.append(
                _issue(
                    Code.UNSUPPORTED_KIND,
                    item.item_id,
                    f"kind {kind!r} is not one of {', '.join(_KINDS)}",
                )
            )
        if (
            len(issues) != problems
            or verdict is None
            or call is None
            or agreement.double_judged == 0
        ):
            continue
        events.append(
            ModelDelegationAcceptanceJudgedEvent(
                correlation_id=call.correlation_id,
                tenant_id=judge.tenant_id,
                delegated_model_key=call.delegated_model_key,
                delegated_tier=call.delegated_tier,
                task_type=item.task_type,
                kind="edit_loop_turn" if kind == "edit_loop_turn" else "task",
                call_time=call.call_time,
                judge_run_id=judge.judge_run_id,
                judge_model=judge.judge_model,
                judge_model_version=judge.judge_model_version,
                rubric_id=judge.rubric_id,
                rubric_version=rubric.rubric_version,
                rubric_hash=judge.rubric_hash,
                calibration_run_id=judge.calibration_run_id,
                calibration_n=agreement.double_judged,
                calibration_agreement=agreement.agreement,
                calibration_kappa=agreement.kappa,
                accept=verdict.accept,
                quality=verdict.quality,
                failure_class=verdict.failure_class,
                judged_at=judge.judged_at,
                origin=judge.origin,
                eval_run_id=judge.eval_run_id,
            )
        )
    return (events, issues) if not issues else ([], issues)
