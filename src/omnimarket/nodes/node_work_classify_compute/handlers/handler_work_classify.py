# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify one work unit by the rubric's first-match rules and the run-style rules (OMN-22263).

Pure compute. A unit no rule decides is UNDETERMINED with a cause from the rubric's data; the
lab-model step is a separate effect node that only an undetermined unit may reach.
"""

from __future__ import annotations

import re
from collections import Counter

from omnimarket.models.work_classification.enum_run_style import EnumRunStyle
from omnimarket.models.work_classification.enum_work_type import EnumWorkType
from omnimarket.models.work_classification.model_work_classification import (
    EnumDecidedBy,
    ModelWorkClassification,
)
from omnimarket.nodes.node_work_classify_compute.handlers.rule_engine import (
    decide_by_classifier,
    decide_by_rules,
    decide_run_style,
)
from omnimarket.nodes.node_work_classify_compute.models import (
    ModelBriefExtractRequest,
    ModelBriefFeatures,
    ModelCellCount,
    ModelClassificationReport,
    ModelClassificationReportRequest,
    ModelMonthShare,
    ModelSessionMeasure,
    ModelStratumCount,
    ModelUnitCell,
    ModelUnitCellRequest,
    ModelWorkClassifyRequest,
    ModelWorkTypeRubric,
)


def brief_stratum(rubric: ModelWorkTypeRubric, features: ModelBriefFeatures) -> str:
    """The first stratum whose declared limits all hold. It names no band and no score."""
    for stratum in rubric.brief_strata:
        if (
            (
                stratum.max_repositories is None
                or features.repositories_named <= stratum.max_repositories
            )
            and (
                stratum.max_length_chars is None
                or features.length_chars <= stratum.max_length_chars
            )
            and (
                stratum.max_criteria is None
                or features.criteria_count <= stratum.max_criteria
            )
        ):
            return stratum.id
    return rubric.brief_strata[-1].id


class HandlerBriefFeatureExtract:
    """Read brief features with the rubric's patterns. Neither the text nor its words are retained, only the words the rules read."""

    def handle(self, request: ModelBriefExtractRequest) -> ModelBriefFeatures:
        patterns = request.rubric.brief_patterns
        text = request.text
        read_by_rules = {
            word for rule in request.rubric.rules for word in rule.match.brief_words
        }
        return ModelBriefFeatures(
            words=frozenset(re.findall(patterns.brief_word, text.lower()))
            & read_by_rules,
            repositories_named=len(
                {
                    m.lower()
                    for m in re.findall(
                        patterns.repositories_named, text, re.IGNORECASE
                    )
                }
            ),
            length_chars=len(text),
            criteria_count=len(re.findall(patterns.criteria_line, text, re.MULTILINE)),
            falsifier_present=re.search(patterns.falsifier, text, re.IGNORECASE)
            is not None,
            premises_verified=len(re.findall(patterns.premise_verified, text)),
            premises_unverified=len(re.findall(patterns.premise_unverified, text)),
        )


class HandlerWorkClassify:
    """Rules first; no default type for a unit they leave open."""

    def handle(self, request: ModelWorkClassifyRequest) -> ModelWorkClassification:
        rubric = request.rubric
        decision = None
        if request.has_intent_text:
            decision = decide_by_rules(rubric, request.inputs) or decide_by_classifier(
                rubric, request.classifier_outputs, request.admitted_classifiers
            )
        style = decide_run_style(request.run_style_rules, request.signals)
        stratum = (
            brief_stratum(rubric, request.brief)
            if request.route is None and request.brief is not None
            else None
        )
        if decision is not None:
            work_type = EnumWorkType(decision.work_type)
            decided_by = EnumDecidedBy.RULE
            rule_id: str | None = decision.rule_id
            cause = None
        else:
            work_type = EnumWorkType(rubric.undetermined_id)
            decided_by = EnumDecidedBy.NONE
            rule_id = None
            cause = (
                rubric.causes.no_rule
                if request.has_intent_text
                else rubric.causes.no_intent_text
            )
        return ModelWorkClassification(
            unit_id=request.unit_id,
            work_type=work_type,
            decided_by=decided_by,
            rule_id=rule_id,
            cause=cause,
            run_style=EnumRunStyle(style) if style is not None else None,
            route=request.route,
            brief_stratum=stratum,
            intent_digest=request.intent_digest,
            rubric_version=rubric.rubric_version,
            route_model_version=None,
            route_receipt_id=None,
        )


def _root(sessions: tuple[ModelSessionMeasure, ...]) -> ModelSessionMeasure | None:
    """The earliest top-level session that carries a brief, else the earliest top-level session."""
    top = sorted((s for s in sessions if s.top_level), key=lambda s: s.started_at)
    briefed = [s for s in top if s.carries_brief]
    if briefed:
        return briefed[0]
    return top[0] if top else None


class HandlerUnitCell:
    """A unit keeps its root session's cell; every session adds its measures."""

    def handle(self, request: ModelUnitCellRequest) -> ModelUnitCell:
        root = _root(request.sessions)
        return ModelUnitCell(
            unit_id=request.unit_id,
            root_session_id=root.session_id if root else None,
            band=root.route.band if root and root.route else None,
            model=root.model if root else None,
            effort=root.effort if root else None,
            tokens=sum(s.tokens for s in request.sessions),
            minutes=sum(s.minutes for s in request.sessions),
            tool_calls=sum(s.tool_calls for s in request.sessions),
            session_count=len(request.sessions),
        )


__all__ = [
    "HandlerBriefFeatureExtract",
    "HandlerClassificationReport",
    "HandlerUnitCell",
    "HandlerWorkClassify",
    "brief_stratum",
]


def _share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


class HandlerClassificationReport:
    """Fold the classified units of a window into the lab-step figures."""

    def handle(
        self, request: ModelClassificationReportRequest
    ) -> ModelClassificationReport:
        months: dict[str, list[int]] = {}
        cells: Counter[tuple[str, str]] = Counter()
        strata: Counter[str | None] = Counter()
        by_rule = by_lab = undetermined = 0
        for item in request.items:
            result = item.classification
            tally = months.setdefault(item.month, [0, 0])
            tally[0] += 1
            if result.decided_by is EnumDecidedBy.RULE:
                by_rule += 1
            elif result.decided_by is EnumDecidedBy.LAB_ROUTE:
                by_lab += 1
            else:
                undetermined += 1
            if result.route is not None:
                tally[1] += 1
                cells[(result.work_type.value, result.route.band)] += 1
            else:
                strata[result.brief_stratum] += 1
        total = len(request.items)
        return ModelClassificationReport(
            units=total,
            decided_by_rule=by_rule,
            decided_by_lab_route=by_lab,
            undetermined=undetermined,
            rule_share=_share(by_rule, total),
            lab_route_share=_share(by_lab, total),
            route_bearing_by_month=tuple(
                ModelMonthShare(
                    month=month,
                    units=units,
                    route_bearing=bearing,
                    share=_share(bearing, units),
                )
                for month, (units, bearing) in sorted(months.items())
            ),
            rated_cells=tuple(
                ModelCellCount(work_type=work_type, band=band, units=count)
                for (work_type, band), count in sorted(cells.items())
            ),
            unrated_strata=tuple(
                ModelStratumCount(stratum=stratum, units=count)
                for stratum, count in sorted(
                    strata.items(), key=lambda pair: str(pair[0])
                )
            ),
        )
