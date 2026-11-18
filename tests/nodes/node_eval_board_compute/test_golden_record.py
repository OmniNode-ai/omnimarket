# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24174 AC4 and AC5: the rendered record matches its golden file and keeps the tab's rules."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from omnimarket.enums.enum_eval_run_state import EnumEvalRunState
from omnimarket.nodes.node_eval_board_compute.handlers import HandlerEvalBoard
from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_overlay import (
    ModelEvalBoardOverlay,
)
from tests.nodes.node_eval_board_compute.support import (
    FIRST_TEST,
    HOSTED_ID,
    LOCAL_ID,
    golden_request,
    live_week,
    model,
    overlay,
    request,
    run,
)

pytestmark = pytest.mark.unit

GOLDEN = Path(__file__).parent / "golden" / "model_tests_record.json"
# Written apart from the overlay's own pattern, so a loosened pattern cannot pass its own test.
ID_TOKEN = re.compile(
    r"OMN-\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{12,}|live-\d{4}-W\d{2}|node_|onex\.|qwen-local|hosted-coder-5|sample-[ab]|rev-\d|settings-\d|prompt-\d|scorer-\d"
)


def _record() -> dict[str, Any]:
    return HandlerEvalBoard().handle(golden_request()).model_dump(mode="json")


def test_the_record_matches_its_golden_file() -> None:
    assert _record() == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_the_golden_comparison_fails_on_a_changed_record() -> None:
    changed = _record()
    changed["chart1"]["series"][0]["points"][0]["rate"]["passed"] += 1
    assert changed != json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_no_text_carries_an_id_shaped_token() -> None:
    text = json.dumps(_record())
    assert ID_TOKEN.search(text) is None


def test_the_id_scan_finds_an_id_when_one_is_planted() -> None:
    planted = _record()
    planted["headline"] += " OMN-24174"
    assert ID_TOKEN.search(json.dumps(planted)) is not None


def test_chart_one_has_no_ninety_percent_line() -> None:
    chart = _record()["chart1"]
    assert set(chart) == {"title", "above", "below", "series"}
    assert "90" not in json.dumps(chart)


def test_each_chart_has_at_most_one_sentence_above_and_below() -> None:
    record = _record()
    for key in ("chart1", "chart3"):
        for part in ("above", "below"):
            assert len(re.findall(r"[.!?](?:\s|$)", record[key][part])) <= 1, (
                key,
                part,
            )
    assert len(re.findall(r"[.!?](?:\s|$)", record["live_work"]["above"])) <= 1


def test_every_percentage_sits_beside_its_count_and_range() -> None:
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "percent" in node:
                assert {
                    "n",
                    "passed",
                    "likely_low",
                    "likely_high",
                    "too_few",
                    "text",
                } <= set(node)
                if node["too_few"]:
                    assert node["percent"] is None
                    assert "%" not in node["text"]
                else:
                    assert node["text"].startswith(
                        f"passed {node['passed']} of {node['n']} tasks ("
                    )
                    assert (
                        f"likely {node['likely_low']} to {node['likely_high']}%"
                        in node["text"]
                    )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(_record())


def test_the_glossary_has_its_four_words_from_the_overlay() -> None:
    record = _record()
    assert [d["term"] for d in record["glossary"]] == [
        "task",
        "pass",
        "size",
        "likely range",
    ]
    assert (
        record["glossary"][3]["definition"]
        == "The 95 percent range of rates that fit the result."
    )


def test_the_footer_keeps_the_publish_time_and_each_models_last_test_apart() -> None:
    footer = _record()["footer"]
    assert footer["published_text"] == "Page data published 2026-11-14 16:50 UTC."
    assert [t["text"] for t in footer["last_tested"]] == [
        "Local coding model (Qwen) last tested 2026-11-30.",
        "Hosted coding model last tested 2026-11-30.",
    ]


def test_the_overlay_refuses_an_id_in_a_label_or_a_missing_glossary_word() -> None:
    with pytest.raises(ValueError, match="id-shaped"):
        overlay(display_names={LOCAL_ID: "Model OMN-24174"})
    with pytest.raises(ValueError, match="exactly"):
        overlay(glossary={"task": "one"})
    assert isinstance(overlay(), ModelEvalBoardOverlay)


def test_every_overlay_model_with_a_closed_run_has_a_chart_one_series() -> None:
    """AC5 on a recorded snapshot: the record's series are the overlay's models that have a closed run."""
    record = HandlerEvalBoard().handle(
        request(
            model(LOCAL_ID, (run("r1", FIRST_TEST),)),
            model(
                HOSTED_ID,
                (
                    run(
                        "r2",
                        FIRST_TEST,
                        state=EnumEvalRunState.VOID,
                        void_reason="cancelled",
                    ),
                ),
            ),
            model("unlisted-model", (run("r3", FIRST_TEST),)),
        )
    )
    assert [s.model_label for s in record.chart1.series] == [
        "Local coding model (Qwen)"
    ]
    assert record.footer.void_notes == (
        "Hosted coding model's test on 2026-11-02 was not counted: cancelled.",
    )


def test_the_model_cap_and_overlay_order_decide_who_is_drawn() -> None:
    record = HandlerEvalBoard().handle(
        request(
            model(HOSTED_ID, (run("r2", FIRST_TEST),)),
            model(LOCAL_ID, (run("r1", FIRST_TEST),)),
            max_models=1,
        )
    )
    assert [s.model_label for s in record.chart1.series] == [
        "Local coding model (Qwen)"
    ]


def test_a_snapshot_with_no_closed_run_has_a_plain_headline_and_empty_charts() -> None:
    record = HandlerEvalBoard().handle(request(model(LOCAL_ID)))
    assert record.headline == "No model has a finished weekly test yet."
    assert record.chart1.series == ()
    assert record.chart3.rows == ()
    assert (
        record.footer.last_tested[0].text
        == "Local coding model (Qwen) has no finished test yet."
    )


def test_a_live_week_reaches_the_record_as_its_monday_never_as_the_live_run_id() -> (
    None
):
    record = HandlerEvalBoard().handle(
        request(
            model(
                LOCAL_ID,
                (run("r1", FIRST_TEST),),
                (live_week("live-2026-W46", 20, 15),),
            )
        )
    )
    (row,) = record.live_work.rows
    assert row.week == "2026-11-09"
    assert ID_TOKEN.search(json.dumps(record.model_dump(mode="json"))) is None


def test_a_week_that_is_not_an_iso_week_reads_as_the_day_it_was_first_scored() -> None:
    record = HandlerEvalBoard().handle(
        request(
            model(LOCAL_ID, (run("r1", FIRST_TEST),), (live_week("live-week", 20, 15),))
        )
    )
    assert record.live_work.rows[0].week == "2026-11-02"


def test_the_overlay_refuses_a_live_run_id_in_a_label() -> None:
    with pytest.raises(ValueError, match="id-shaped"):
        overlay(display_names={LOCAL_ID: "Model live-2026-W46"})
