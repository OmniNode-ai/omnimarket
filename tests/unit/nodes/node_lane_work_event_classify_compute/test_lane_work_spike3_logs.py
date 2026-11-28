# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The classifier names the four real red CI job logs of spike 3 (OMN-26698).

The fixtures are the failing step of each job as the CI log recorded it, timestamps included:
omnimarket#6308 (job 136763549829), #6573 (139519889541), #6700 (139037313314) and #6802
(140182703055). Every rule is overlay data; the handler holds no pattern, kind or length.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_lane_work_event import EnumLaneWorkEvent
from omnibase_core.enums.enum_lane_work_state import EnumLaneWorkState
from omnibase_core.models.lane_work import ModelLaneWorkChange, ModelLaneWorkState

from omnimarket.nodes.node_ci_red_kind_rule_compute.handlers.handler_ci_red_kind_rule import (
    HandlerCiRedKindRule,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_overlay import (
    ModelCiRedKindRuleOverlay,
)
from omnimarket.nodes.node_lane_work_event_classify_compute.handlers.handler_lane_work_event_classify import (
    HandlerLaneWorkEventClassify,
)
from omnimarket.nodes.node_lane_work_event_classify_compute.models.model_lane_work_classify_overlay import (
    ModelLaneWorkClassifyOverlay,
)

pytestmark = pytest.mark.unit

E = EnumLaneWorkEvent
ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "tests" / "fixtures" / "lane_work_spike3" / "2026-11-27"
NODE = ROOT / "src" / "omnimarket" / "nodes" / "node_lane_work_event_classify_compute"
HEAD = "0dbe3156e52dcb33b15fc3c82f5959de6691a4de"
STAMP = re.compile(r"(?m)^\d{4}-\d\d-\d\dT[\d:.]+Z ")

LOGS = {
    "6308": "omnimarket_6308_delegation_health_job_136763549829.txt",
    "6573": "omnimarket_6573_canonical_file_shape_job_139519889541.txt",
    "6700": "omnimarket_6700_overlay_main_load_job_139037313314.txt",
    "6802": "omnimarket_6802_overlay_main_load_job_140182703055.txt",
}


def log(name: str) -> str:
    return (FIXTURES / LOGS[name]).read_text()


def overlay_doc() -> dict[str, Any]:
    doc = yaml.safe_load((NODE / "overlay.example.yaml").read_text())
    assert isinstance(doc, dict)
    return doc


def make(overlay: dict[str, Any] | None = None) -> HandlerLaneWorkEventClassify:
    parsed = ModelLaneWorkClassifyOverlay.model_validate(overlay or overlay_doc())
    # The red kind node holds one rule here, as a bound overlay with no rule for these logs would.
    red = HandlerCiRedKindRule(
        load_overlay=lambda: ModelCiRedKindRuleOverlay.model_validate(
            {
                "schema_version": "ci_red_kind_rules.v1",
                "rules": [
                    {
                        "kind": "base_inherited",
                        "log_pattern": "fails on dev itself",
                        "ignore_case": True,
                    }
                ],
            }
        )
    )
    return HandlerLaneWorkEventClassify(load_overlay=lambda: parsed, red_kind_rule=red)


def state() -> ModelLaneWorkState:
    return ModelLaneWorkState(
        workflow_id="omninode-ai-omnimarket-6700-1",
        change=ModelLaneWorkChange(
            owner="OmniNode-ai", repo="omnimarket", pr_number=6700, ticket="OMN-26043"
        ),
        state=EnumLaneWorkState.RED,
        seq=4,
        expected_head=HEAD,
        worker_lane="red6700-1",
    )


def read(raw: str, overlay: dict[str, Any] | None = None) -> Any:
    event = make(overlay).classify(raw_result=raw, state=state())
    assert event is not None
    return event


@pytest.mark.parametrize(
    ("change", "red_kind"),
    [
        ("6308", "foreign_repo_verdict"),
        ("6573", "file_shape_ratchet"),
        ("6700", "overlay_required"),
        ("6802", "overlay_required"),
    ],
)
def test_each_real_red_log_is_a_log_read_of_a_named_kind(
    change: str, red_kind: str
) -> None:
    event = read(log(change))
    assert (event.kind, event.red_kind) == (E.LOG_READ, red_kind)


def test_the_fixtures_are_timestamped_ci_lines() -> None:
    for change in LOGS:
        assert STAMP.search(log(change)), change


def test_a_ci_file_shape_log_is_not_a_commit_refusal() -> None:
    assert read(log("6573")).kind is not E.COMMIT_REFUSED


def test_the_same_text_from_a_commit_hook_stays_a_commit_refusal() -> None:
    hook = "Canonical-file-shape ratchet (OMN-20304) refused this change:\n  bin/x: [noncanonical-file]\n"
    event = read(hook)
    assert (event.kind, event.event_class) == (E.COMMIT_REFUSED, "hook")


@pytest.mark.parametrize("change", ["6308", "6700", "6802"])
def test_rules_match_lines_with_their_timestamps_stripped(change: str) -> None:
    bare = STAMP.sub("", log(change))
    assert not STAMP.search(bare)
    as_logged = read(log(change))
    event = read(bare)
    assert as_logged.kind is E.LOG_READ
    assert (event.kind, event.red_kind) == (as_logged.kind, as_logged.red_kind)


def test_evidence_is_kept_whole_up_to_the_overlay_cap() -> None:
    raw = log("6802")
    assert len(raw) > 1000
    assert raw.index("overlay-main-load: loaded") > 1000
    event = read(raw)
    assert event.evidence[0] == raw
    assert "overlay-main-load: loaded=370 refused=1" in event.evidence[0]


def test_the_evidence_cap_is_the_overlays_and_not_a_literal() -> None:
    doc = overlay_doc()
    doc["evidence_limit"] = 120
    assert len(read(log("6802"), doc).evidence[0]) == 120
    doc["evidence_limit"] = 50000
    assert len(read("x" * 20000, doc).evidence[0]) == 20000


def test_a_failed_verdict_of_this_repository_is_not_a_foreign_one() -> None:
    own = (
        "2026-11-21T06:44:41.3480771Z ##[error]workflow verdict FAILED for "
        "OmniNode-ai/omnimarket delegation-regression-nightly.yml on dev\n"
    )
    assert read(own).kind is E.UNCLASSIFIED


def test_the_handler_holds_no_rule_literal() -> None:
    source = (NODE / "handlers" / "handler_lane_work_event_classify.py").read_text()
    for literal in (
        "foreign_repo_verdict",
        "file_shape_ratchet",
        "overlay_required",
        "1000",
    ):
        assert literal not in source, literal
