# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shipped browser reducer agrees with the typed compute on selected windows."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.process_health import ModelTimeSpan
from omnimarket.nodes.node_process_health_compute.handlers.handler_process_health import (
    HandlerProcessHealth,
)
from omnimarket.nodes.node_process_health_compute.tests.fixtures import at, request

PAGE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/reports/process-health/index.html"
)
HARNESS = r"""
const fs=require('fs');
const [path]=process.argv.slice(1);
const {data,commands}=JSON.parse(fs.readFileSync(0,'utf8'));
const script=/<script>([\s\S]*?)<\/script>/.exec(fs.readFileSync(path,'utf8'))[1];
const h=JSON.parse(data);
const document={getElementById:()=>({}),documentElement:{}};
const api=new Function('document',script.slice(0,script.indexOf('el("measure").onchange'))+
  ';return {compareRange,valueFor,fromEastern,rangeFor,easternInput,setRange:r=>selectedRange=r};')(document);
const out=JSON.parse(commands).map(c=>{
  if(c.op==='compare')return api.compareRange(h,{start:Date.parse(c.start),end:Date.parse(c.end)});
  if(c.op==='value')return api.valueFor(h,h.days.at(-1).window.metrics.find(m=>m.key===c.key),Date.parse(c.start),Date.parse(c.end),c.enforce!==false);
  if(c.op==='month'){api.setRange('month');return api.rangeFor({...h,generated_at:c.end});}
  return api.fromEastern(c.value);
});
console.log(JSON.stringify(out));
"""


def run_browser_logic(commands: list[dict[str, Any]], **overrides: Any) -> list[Any]:
    node = shutil.which("node")
    assert node, "The Process page behaviour tests require Node.js"
    result = HandlerProcessHealth().handle(request(**overrides))
    done = subprocess.run(
        [node, "-e", HARNESS, str(PAGE)],
        input=json.dumps(
            {"data": result.model_dump_json(), "commands": json.dumps(commands)}
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(done.stdout)


def test_selected_range_changes_browser_counts() -> None:
    first, second = run_browser_logic(
        [
            {
                "op": "compare",
                "start": at(29, 0).isoformat(),
                "end": at(30, 0).isoformat(),
            },
            {
                "op": "compare",
                "start": at(28, 0).isoformat(),
                "end": at(29, 0).isoformat(),
            },
        ]
    )
    fields = ("improved", "worse", "flat", "unmeasured")
    assert tuple(first[k] for k in fields) != tuple(second[k] for k in fields)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (at(28, 0), at(29, 0)),
        (at(29, 0), at(30, 0)),
        (at(28, 12), at(29, 12)),
        (at(29, 1, 30), at(30, 12)),
    ],
)
def test_browser_values_and_verdicts_match_canonical_compute(
    start: Any, end: Any
) -> None:
    expected = (
        HandlerProcessHealth()
        .handle(request(selected_range=ModelTimeSpan(start=start, end=end)))
        .selected_summary
    )
    actual = run_browser_logic(
        [{"op": "compare", "start": start.isoformat(), "end": end.isoformat()}]
    )[0]
    by_key = {c["key"]: c for c in actual["comparisons"]}
    for comparison in expected.comparisons:
        found = by_key[comparison.key]
        assert found["before"] == pytest.approx(comparison.before, abs=0.001), (
            comparison.key
        )
        assert found["after"] == pytest.approx(comparison.after, abs=0.001), (
            comparison.key
        )
        assert found["verdict"] == comparison.verdict.value, comparison.key
    for key in ("improved", "worse", "flat", "unmeasured"):
        assert actual[key] == getattr(expected, key)


def test_partial_and_sub_bucket_ranges_are_unmeasured() -> None:
    results = run_browser_logic(
        [
            {
                "op": "value",
                "key": "validation.refusals",
                "start": at(27, 12).isoformat(),
                "end": at(28, 12).isoformat(),
            },
            {
                "op": "value",
                "key": "validation.refusals",
                "start": at(28, 10).isoformat(),
                "end": at(28, 11).isoformat(),
            },
        ]
    )
    assert all(result["value"] is None for result in results)
    assert "partly" in results[0]["reason"]
    assert "shorter" in results[1]["reason"]


def test_custom_eastern_times_reject_dst_gap_and_ambiguity() -> None:
    gap, repeated, winter, summer = run_browser_logic(
        [
            {"op": "eastern", "value": "2026-03-08T02:30"},
            {"op": "eastern", "value": "2026-11-01T01:30"},
            {"op": "eastern", "value": "2026-11-04T12:00"},
            {"op": "eastern", "value": "2026-10-28T12:00"},
        ]
    )
    assert gap is None
    assert repeated is None
    assert (winter - summer) / 3600000 == 7 * 24 + 1


def test_calendar_month_clamps_end_of_month() -> None:
    result = run_browser_logic([{"op": "month", "end": "2026-03-31T16:00:00Z"}])[0]
    # Noon Eastern in March is EDT; February is EST.
    assert result["start"] == 1772298000000  # 2026-02-28T17:00:00Z
    assert (
        result["end"] - result["start"]
        == timedelta(days=30, hours=23).total_seconds() * 1000
    )


def test_partial_source_declaration_cannot_make_a_range_better() -> None:
    from omnimarket.models.process_health import EnumReadStatus, EnumSourceName

    base = request()
    sources = tuple(
        source.model_copy(
            update={
                "status": EnumReadStatus.PARTIAL,
                "reason": "incomplete archive read",
            }
        )
        if source.name is EnumSourceName.LEDGER
        else source
        for source in base.sources
    )
    actual = run_browser_logic(
        [
            {
                "op": "compare",
                "start": at(29, 0).isoformat(),
                "end": at(30, 0).isoformat(),
            }
        ],
        sources=sources,
    )[0]
    metric = next(c for c in actual["comparisons"] if c["key"] == "andon.stops_started")
    assert metric["verdict"] == "unmeasured"
    assert metric["after"] is None
    summary = (
        HandlerProcessHealth()
        .handle(
            request(
                sources=sources,
                selected_range=ModelTimeSpan(start=at(29, 0), end=at(30, 0)),
            )
        )
        .selected_summary
    )
    assert (
        next(c for c in summary.comparisons if c.key == "andon.stops_started").after
        is None
    )


def test_unknown_cost_is_unmeasured_instead_of_zero() -> None:
    from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow

    unknown = ModelLandingLedgerRow(
        ts="2026-10-28T11:00:00Z",
        rtype="FRICTION",
        lane="unknown-cost",
        text="lane=unknown-cost | actor=worker | class=refusal | cost=unknown | no measurement",
    )
    measured, unmeasured = run_browser_logic(
        [
            {
                "op": "value",
                "key": "friction.lane_stated_minutes",
                "start": at(29, 0).isoformat(),
                "end": at(30, 0).isoformat(),
            },
            {
                "op": "value",
                "key": "friction.lane_stated_minutes",
                "start": at(28, 0).isoformat(),
                "end": at(29, 0).isoformat(),
            },
        ],
        ledger_rows=(*request().ledger_rows, unknown),
    )
    assert measured["value"] == 60
    assert unmeasured["value"] is None
    assert "parseable cost" in unmeasured["reason"]
