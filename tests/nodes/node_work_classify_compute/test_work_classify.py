# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC5, AC7, AC9, AC10, AC12: rule classification, strata, no default band, the root session's cell (OMN-22263)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.work_classification.enum_run_style import EnumRunStyle
from omnimarket.models.work_classification.enum_work_type import EnumWorkType
from omnimarket.models.work_classification.model_work_classification import (
    EnumDecidedBy,
    ModelRouteDimensions,
    ModelRouteLine,
    ModelWorkClassification,
)
from omnimarket.nodes.node_work_classify_compute.handlers.handler_work_classify import (
    HandlerBriefFeatureExtract,
    HandlerClassificationReport,
    HandlerUnitCell,
    HandlerWorkClassify,
)
from omnimarket.nodes.node_work_classify_compute.handlers.rubric_loader import (
    load_rubric,
    load_run_style_rules,
)
from omnimarket.nodes.node_work_classify_compute.models import (
    ModelBriefExtractRequest,
    ModelClassificationReportRequest,
    ModelReportItem,
    ModelRuleInputs,
    ModelSessionMeasure,
    ModelUnitCellRequest,
    ModelWorkClassifyRequest,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
CONTRACTS = REPO / "src" / "omnimarket" / "contracts"
RUBRIC = load_rubric(
    (CONTRACTS / "work_type_rubric.v1.yaml").read_text(encoding="utf-8"),
    expected_ids={m.value for m in EnumWorkType},
    task_class_names=set(
        yaml.safe_load(
            (REPO / "src/omnimarket/configs/task_class_contracts.v1.yaml").read_text(
                encoding="utf-8"
            )
        )["task_classes"]
    ),
)
STYLES = load_run_style_rules(
    (CONTRACTS / "run_style_rules.v1.yaml").read_text(encoding="utf-8"),
    expected_styles={m.value for m in EnumRunStyle},
)
ROUTE = ModelRouteLine(
    band="B3",
    score=13,
    dimensions=ModelRouteDimensions(values=(("S", 13),)),
    dest="claude",
    model="sonnet",
    effort="high",
)


def _request(**over: Any) -> ModelWorkClassifyRequest:
    base: dict[str, Any] = {
        "unit_id": "u1",
        "inputs": ModelRuleInputs(
            lane_name="fix-null-cell",
            ticket_class=None,
            route_dest=None,
            brief_words=None,
        ),
        "route": None,
        "brief": None,
        "intent_digest": "sha256:abc",
        "has_intent_text": True,
        "signals": {},
        "classifier_outputs": {},
        "admitted_classifiers": (),
        "rubric": RUBRIC,
        "run_style_rules": STYLES,
    }
    base.update(over)
    return ModelWorkClassifyRequest(**base)


def test_a_rule_decides_and_names_itself() -> None:
    result = HandlerWorkClassify().handle(_request())
    assert result.work_type is EnumWorkType.FIX
    assert result.decided_by is EnumDecidedBy.RULE
    assert result.rule_id == "fix-lane-token"
    assert result.cause is None


def test_an_open_unit_is_undetermined_never_a_guessed_type() -> None:
    result = HandlerWorkClassify().handle(
        _request(
            inputs=ModelRuleInputs(
                lane_name="lane-aa-1",
                ticket_class=None,
                route_dest=None,
                brief_words=None,
            )
        )
    )
    assert result.work_type is EnumWorkType.UNDETERMINED
    assert result.decided_by is EnumDecidedBy.NONE
    assert result.cause == RUBRIC.causes.no_rule


def test_a_session_with_no_intent_text_is_undetermined_with_that_cause() -> None:
    result = HandlerWorkClassify().handle(_request(has_intent_text=False))
    assert result.work_type is EnumWorkType.UNDETERMINED
    assert result.cause == RUBRIC.causes.no_intent_text


def test_an_excluded_classifier_is_never_read_and_an_admitted_one_decides() -> None:
    open_inputs = ModelRuleInputs(
        lane_name="lane-aa-1", ticket_class=None, route_dest=None, brief_words=None
    )
    outputs = {"node_ticket_classify_compute": "bug"}
    excluded = HandlerWorkClassify().handle(
        _request(
            inputs=open_inputs, classifier_outputs=outputs, admitted_classifiers=()
        )
    )
    assert excluded.work_type is EnumWorkType.UNDETERMINED
    admitted = HandlerWorkClassify().handle(
        _request(
            inputs=open_inputs,
            classifier_outputs=outputs,
            admitted_classifiers=("node_ticket_classify_compute",),
        )
    )
    assert admitted.work_type is EnumWorkType.FIX
    assert admitted.rule_id == "node_ticket_classify_compute"


def test_a_unit_without_a_route_line_carries_route_none_a_stratum_and_no_band() -> None:
    brief = HandlerBriefFeatureExtract().handle(
        ModelBriefExtractRequest(
            text="Build the thing in omnialpha.\n- [ ] AC1: x", rubric=RUBRIC
        )
    )
    result = HandlerWorkClassify().handle(_request(brief=brief))
    assert result.route is None
    assert result.brief_stratum == RUBRIC.brief_strata[0].id
    assert result.model_dump()["route"] is None


def test_a_rated_unit_takes_no_stratum() -> None:
    brief = HandlerBriefFeatureExtract().handle(
        ModelBriefExtractRequest(text="short", rubric=RUBRIC)
    )
    result = HandlerWorkClassify().handle(_request(route=ROUTE, brief=brief))
    assert result.route == ROUTE
    assert result.brief_stratum is None


def test_the_stratum_follows_the_rubric_edges() -> None:
    text = "omnialpha omnibeta omnigamma omnidelta\n" + "\n".join(
        f"AC{i}: criterion" for i in range(1, 14)
    )
    brief = HandlerBriefFeatureExtract().handle(
        ModelBriefExtractRequest(text=text, rubric=RUBRIC)
    )
    assert brief.repositories_named == 4
    assert brief.criteria_count == 13
    result = HandlerWorkClassify().handle(_request(brief=brief))
    assert result.brief_stratum == RUBRIC.brief_strata[-1].id


def test_brief_features_read_falsifier_and_premise_marks_and_hold_no_text() -> None:
    text = (
        "Premise: x verified by: grep => 1\nPremise: y UNVERIFIED\n"
        "falsifier: a planted token UNIQUETOKEN"
    )
    features = HandlerBriefFeatureExtract().handle(
        ModelBriefExtractRequest(text=text, rubric=RUBRIC)
    )
    assert features.falsifier_present is True
    assert (features.premises_verified, features.premises_unverified) == (1, 1)
    assert "UNIQUETOKEN" not in features.model_dump_json()
    result = HandlerWorkClassify().handle(_request(brief=features))
    assert "UNIQUETOKEN" not in result.model_dump_json()


def test_the_request_serialisation_drops_the_brief_text() -> None:
    request = ModelBriefExtractRequest(text="UNIQUETOKEN-REQ", rubric=RUBRIC)
    assert "UNIQUETOKEN-REQ" not in request.model_dump_json()
    assert "UNIQUETOKEN-REQ" not in repr(request)


def test_the_run_style_comes_from_the_signals() -> None:
    result = HandlerWorkClassify().handle(
        _request(signals={"workflow_run_id": "present"})
    )
    assert result.run_style is EnumRunStyle.WORKFLOW
    none = HandlerWorkClassify().handle(_request(signals={}))
    assert none.run_style is None


def _session(
    sid: str, at: str, *, top: bool, brief: bool, model: str, tokens: int
) -> ModelSessionMeasure:
    return ModelSessionMeasure(
        session_id=sid,
        started_at=at,
        top_level=top,
        carries_brief=brief,
        route=ROUTE if model == "sonnet" else None,
        model=model,
        effort="high",
        tokens=tokens,
        minutes=1.0,
        tool_calls=2,
    )


def test_a_lane_with_a_sonnet_root_and_an_opus_second_session_keeps_the_sonnet_cell() -> (
    None
):
    cell = HandlerUnitCell().handle(
        ModelUnitCellRequest(
            unit_id="lane-1",
            sessions=(
                _session(
                    "s2",
                    "2026-11-03T02:00:00Z",
                    top=True,
                    brief=False,
                    model="opus",
                    tokens=700,
                ),
                _session(
                    "s1",
                    "2026-11-03T01:00:00Z",
                    top=True,
                    brief=True,
                    model="sonnet",
                    tokens=300,
                ),
                _session(
                    "s3",
                    "2026-11-03T03:00:00Z",
                    top=False,
                    brief=False,
                    model="haiku",
                    tokens=50,
                ),
            ),
        )
    )
    assert cell.root_session_id == "s1"
    assert cell.model == "sonnet"
    assert cell.band == "B3"
    assert cell.tokens == 1050
    assert cell.session_count == 3


def test_foreground_work_takes_the_earliest_top_level_session() -> None:
    cell = HandlerUnitCell().handle(
        ModelUnitCellRequest(
            unit_id="fg-1",
            sessions=(
                _session(
                    "b",
                    "2026-11-03T02:00:00Z",
                    top=True,
                    brief=False,
                    model="opus",
                    tokens=1,
                ),
                _session(
                    "a",
                    "2026-11-03T01:00:00Z",
                    top=True,
                    brief=False,
                    model="sonnet",
                    tokens=1,
                ),
            ),
        )
    )
    assert cell.root_session_id == "a"


def _classified(
    unit: str,
    *,
    route: ModelRouteLine | None,
    by: EnumDecidedBy,
    stratum: str | None = None,
    work_type: EnumWorkType = EnumWorkType.FIX,
) -> ModelWorkClassification:
    return ModelWorkClassification(
        unit_id=unit,
        work_type=work_type,
        decided_by=by,
        rule_id=None,
        cause=None,
        run_style=None,
        route=route,
        brief_stratum=stratum,
        intent_digest=None,
        rubric_version="1",
        route_model_version=None,
        route_receipt_id=None,
    )


def test_the_report_holds_the_route_bearing_share_by_month_and_the_lab_step_figures() -> (
    None
):
    items = (
        ModelReportItem(
            month="2026-10",
            classification=_classified("a", route=ROUTE, by=EnumDecidedBy.RULE),
        ),
        ModelReportItem(
            month="2026-10",
            classification=_classified(
                "b", route=None, by=EnumDecidedBy.RULE, stratum="brief-small"
            ),
        ),
        ModelReportItem(
            month="2026-10",
            classification=_classified(
                "c", route=None, by=EnumDecidedBy.LAB_ROUTE, stratum="brief-small"
            ),
        ),
        ModelReportItem(
            month="2026-11",
            classification=_classified(
                "d",
                route=ROUTE,
                by=EnumDecidedBy.NONE,
                work_type=EnumWorkType.UNDETERMINED,
            ),
        ),
    )
    report = HandlerClassificationReport().handle(
        ModelClassificationReportRequest(items=items)
    )
    assert (
        report.units,
        report.decided_by_rule,
        report.decided_by_lab_route,
        report.undetermined,
    ) == (4, 2, 1, 1)
    assert report.rule_share == 0.5
    assert report.lab_route_share == 0.25
    by_month = {m.month: m for m in report.route_bearing_by_month}
    assert (by_month["2026-10"].units, by_month["2026-10"].route_bearing) == (3, 1)
    assert by_month["2026-11"].share == 1.0
    assert {(c.work_type, c.band, c.units) for c in report.rated_cells} == {
        ("fix", "B3", 1),
        ("undetermined", "B3", 1),
    }
    assert [(s.stratum, s.units) for s in report.unrated_strata] == [("brief-small", 2)]


def test_an_empty_window_reports_no_shares_not_zero() -> None:
    report = HandlerClassificationReport().handle(
        ModelClassificationReportRequest(items=())
    )
    assert report.rule_share is None
    assert report.route_bearing_by_month == ()
