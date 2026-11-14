# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Delegation tab draws four views from delegation-history.json: models, success over time, experiments, scenarios.

The chain: the history document the data lane's fold produces (the fixture of the seam between the data lane and the page lane,
read through the merged schema model omnimarket.models.floor_delegation_history), the page's own script under node, and the markup
each view leaves. The fields the page reads are recorded while it draws (every property read of the served document goes through a
recording proxy), never listed by hand, and each recorded name is held against the schema model. The failing controls are a history
with one experiment arm missing a field the page reads (the view must say so, never draw a number), an unreadable or stale history
(the views say so and draw no number; the scenarios still draw), and a field the page reads that the schema model does not have.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models import floor_delegation_history as schema
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    load_runtime,
)
from tests.nodes.node_factory_floor_publish_effect.test_compare_page import NODE_DIR
from tests.nodes.node_factory_floor_publish_effect.test_delegation_page import (
    _tracker,
    published_page,
)

pytestmark = pytest.mark.unit

PAGE = NODE_DIR / "page/delegation.html"
FIXTURE = (
    Path(__file__).parents[1]
    / "node_floor_delegation_history_compute/fixtures/delegation-history.fixture.json"
)
HISTORY_NOW = "2026-11-14T03:00:00+00:00"
LIMIT_S = load_runtime().delegation_history_max_age_seconds
# A phone is 360 px wide; the page's own padding (10 px a side) and a card's (14 px a side, 1 px border) leave this for a chart.
PHONE_CHART_PX = 360 - 2 * 10 - 2 * 14 - 2
MIN_TEXT_PX = 9.0

HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, filesPath, nowIso] = process.argv.slice(1);
    // Every property the page reads of the history document is recorded under its section (the schema model that carries it).
    const CHILD = {"top.models": "models", "top.series": "series", "top.experiments": "experiments", "top.sources": "sources",
      "series.days": "points", "series.hours": "points", "series.total": "points", "points.success": "success",
      "experiments.task_set": "task_set", "experiments.majority_baseline": "baseline", "experiments.arms": "arms", "experiments.paired": "paired"};
    const reads = {};
    const wrap = (v, section) => {
      if (v === null || typeof v !== "object") return v;
      return new Proxy(v, {get(t, p, r) {
        const val = Reflect.get(t, p, r);
        if (typeof p !== "string" || p === "then") return val;
        if (Array.isArray(t)) return /^\\d+$/.test(p) ? wrap(val, section) : val;
        (reads[section] = reads[section] || new Set()).add(p);
        const child = CHILD[section + "." + p];
        return child ? wrap(val, child) : val;
      }});
    };
    const files = JSON.parse(fs.readFileSync(filesPath, "utf8"));
    const src = fs.readFileSync(pagePath, "utf8");
    const script = /<script>([\\s\\S]*?)<\\/script>/.exec(src)[1];
    const els = {};
    const mk = id => ({id, innerHTML: "", textContent: "", value: "", dataset: {}, hidden: false, open: false, setAttribute() {},
      removeAttribute() {}, addEventListener() {}, querySelectorAll() { return []; }, showModal() {}, close() {}, focus() {}});
    const handlers = {};
    globalThis.document = {title: "", getElementById: id => els[id] || (els[id] = mk(id)), addEventListener: (t, fn) => { handlers[t] = fn; }};
    globalThis.window = {location: {hash: process.env.PAGE_HASH || ""}, addEventListener: (t, fn) => { handlers["window:" + t] = fn; }};
    const now = Date.parse(nowIso);
    Date.now = () => now;
    globalThis.setInterval = () => 0;
    globalThis.fetch = async url => (url in files
      ? {ok: true, status: 200, json: async () => (url === "delegation-history.json" ? wrap(JSON.parse(files[url]), "top") : JSON.parse(files[url]))}
      : {ok: false, status: 404, json: async () => ({})});
    (async () => {
      new Function(script)();
      await new Promise(r => setTimeout(r, 50));
      for (const v of (process.env.PAGE_CLICKS || "").split(",").filter(Boolean)) handlers.click({target: {closest: () => ({dataset: {view: v}})}});
      if (process.env.PAGE_HASHCHANGE) { window.location.hash = "#" + process.env.PAGE_HASHCHANGE; handlers["window:hashchange"](); }
      const out = {};
      for (const id of ["halert", "models", "msum", "success", "ssumh", "experiments", "esum", "scn", "v-models", "v-success", "v-experiments", "v-scenarios"]) out[id] = els[id] ? (els[id].innerHTML || els[id].textContent) : "";
      out.hidden = Object.fromEntries(["models", "success", "experiments", "scenarios"].map(v => [v, !!(els["v-" + v] && els["v-" + v].hidden)]));
      process.stdout.write(JSON.stringify({els: out, reads: Object.fromEntries(Object.entries(reads).map(([k, v]) => [k, [...v].sort()]))}));
    })();
    """
)


def _history() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _render(
    history: dict | None,
    *,
    tracker: bool = True,
    clicks: str = "",
    now: str | None = None,
    tracker_bytes: bytes | None = None,
    hashchange: str = "",
) -> dict:
    node = shutil.which("node")
    assert node, "the delegation history page render test requires Node.js"
    files: dict[str, str] = {}
    if tracker:
        files["delegation-tracker.json"] = (
            tracker_bytes or _tracker(red_scenario=False)
        ).decode()
    if history is not None:
        files["delegation-history.json"] = json.dumps(history)
    # the history document is large: it goes to the script as a file, never as an argument
    with tempfile.TemporaryDirectory() as tmp:
        files_path = Path(tmp) / "files.json"
        files_path.write_text(json.dumps(files), encoding="utf-8")
        served = Path(tmp) / "delegation.html"
        served.write_bytes(published_page())
        out = subprocess.run(
            [node, "-e", HARNESS, str(served), str(files_path), now or HISTORY_NOW],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
            env={
                "PAGE_CLICKS": clicks,
                "PAGE_HASHCHANGE": hashchange,
                "PATH": "/usr/bin:/bin",
            },
        )
    return json.loads(out.stdout)


def test_the_fixture_conforms_to_the_data_lanes_schema_model() -> None:
    """The page is built against a document that the data lane's own model accepts, so a schema change turns this red."""
    doc = schema.ModelDelegationHistory.model_validate(_history())
    assert doc.schema_id == schema.SCHEMA_ID


def test_golden_every_view_is_drawn_from_the_fixture() -> None:
    doc = _history()
    drawn = _render(doc)["els"]
    assert drawn["halert"] == ""
    # models: one row per model, with engine, host, settings, price, the success over the window and its interval
    for m in doc["models"]:
        assert m["id"] in drawn["models"]
    assert drawn["models"].count("<tr><td") == len(doc["models"])
    assert "no price (local)" in drawn["models"]
    assert "$0.500 in / $2.00 out per M tokens" in drawn["models"]
    assert "131072 ctx, thinking on" in drawn["models"]
    assert "65536 ctx, Q3_K Medium, thinking off" in drawn["models"]
    assert re.search(r"\d+ % to \d+ %, n=\d+", drawn["models"])
    assert f"{len(doc['models'])} models" in drawn["msum"]
    # success over time: one card per task kind (the all-kinds entry is not a card), a polyline and a band per line
    kinds = sorted({r["task_kind"] for r in doc["series"]} - {"*"})
    assert drawn["success"].count('<section class="card">') == len(kinds)
    assert "<polyline" in drawn["success"]
    assert "<polygon" in drawn["success"]
    assert re.search(
        r"<title>Model-X@192\.0\.2\.10 2026-\d\d-\d\d: \d+ % \(n=\d+\)</title>",
        drawn["success"],
    )
    assert f"{len(kinds)} task kinds" in drawn["ssumh"]
    # experiments: a card per experiment, newest first, the verdict where the report states one, the paired tests
    exps = sorted(doc["experiments"], key=lambda e: e["date"], reverse=True)
    assert drawn["experiments"].count('class="card exp"') == len(exps)
    assert drawn["experiments"].index(exps[0]["question"]) < drawn["experiments"].index(
        exps[-1]["question"]
    )
    assert exps[0]["verdict"][:60] in drawn["experiments"]
    assert "no verdict stated" in drawn["experiments"]
    paired = exps[0]["paired"][0]
    assert f"{paired['arm_a']} vs {paired['arm_b']}" in drawn["experiments"]
    assert "exact McNemar" in drawn["experiments"]
    for view in ("models", "success", "experiments"):
        assert "NaN" not in drawn[view]
        assert "undefined" not in drawn[view]
        assert "null" not in drawn[view]
    # scenarios: the nine tracker scenarios are kept, each with its pass history strip
    assert drawn["scn"].count('data-open="scn:') == 9
    assert drawn["scn"].count('class="strip"') >= 1
    assert "runs, " in drawn["scn"]


def test_error_an_unreadable_history_says_so_in_three_views_and_leaves_the_scenarios() -> (
    None
):
    drawn = _render(None)["els"]
    assert "cannot be read (HTTP 404)" in drawn["halert"]
    for view in ("models", "success", "experiments"):
        assert "No history to draw." in drawn[view]
    assert drawn["scn"].count('data-open="scn:') == 9


def _generated(seconds_before_now: int) -> str:
    now = datetime.fromisoformat(HISTORY_NOW)
    return (now - timedelta(seconds=seconds_before_now)).isoformat()


# what a drawn number leaves in the markup of the three views
NUMBER_MARKS = (
    "<tr><td",
    "<polyline",
    "<polygon",
    "<circle",
    'class="card exp"',
    "<rect",
)


def _numbers_drawn(drawn: dict) -> list[str]:
    return [
        f"{view}: {mark}"
        for view in ("models", "success", "experiments")
        for mark in NUMBER_MARKS
        if mark in drawn[view]
    ] + [
        f"{view}: {m}"
        for view in ("models", "success", "experiments")
        for m in re.findall(r"\d+ %", drawn[view])
    ]


def test_error_a_stale_history_is_marked_old_and_draws_no_number() -> None:
    doc = _history()
    doc["generated_at"] = "2026-11-10T00:00:00Z"
    drawn = _render(doc)["els"]
    assert f"4 d old, the limit is {LIMIT_S // 3600} h" in drawn["halert"]
    assert "ago old" not in drawn["halert"]
    for view in ("models", "success", "experiments"):
        assert "too old to draw" in drawn[view]
    assert _numbers_drawn(drawn) == []
    assert drawn["msum"] == drawn["ssumh"] == drawn["esum"] == ""
    # the scenarios, which are the tracker's, still draw
    assert drawn["scn"].count('data-open="scn:') == 9


def test_failing_control_a_history_just_inside_the_limit_is_drawn_so_the_stale_check_can_fail() -> (
    None
):
    """The control of the stale test: the same document one minute inside the limit leaves numbers, so _numbers_drawn can see them."""
    doc = _history()
    doc["generated_at"] = _generated(LIMIT_S - 60)
    drawn = _render(doc)["els"]
    assert drawn["halert"] == ""
    assert _numbers_drawn(drawn) != []
    doc["generated_at"] = _generated(LIMIT_S + 60)
    assert _numbers_drawn(_render(doc)["els"]) == []


def test_the_limit_comes_from_the_node_contract_not_the_page() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert re.search(r"HIST_MAX_AGE_S = __HISTORY_MAX_AGE_S__;", page)
    assert not re.search(r"HIST_MAX_AGE_S = \d", page)
    assert "|| 14" not in page
    assert str(LIMIT_S).encode() in published_page()
    assert b"__HISTORY_MAX_AGE_S__" not in published_page()


def test_error_a_history_with_another_schema_id_is_refused_not_drawn() -> None:
    doc = _history()
    doc["schema_id"] = "omnimarket.floor_delegation_history.v0"
    drawn = _render(doc)["els"]
    assert "no renderable shape, schema id or generated_at" in drawn["halert"]
    assert "No history to draw." in drawn["models"]


def test_error_a_source_that_failed_is_named_in_the_banner() -> None:
    doc = _history()
    doc["sources"][1] = {"id": "routing_overlay", "ok": False, "detail": "unreadable"}
    banner = _render(doc)["els"]["halert"]
    assert "routing_overlay" in banner
    assert "unreadable" in banner


def test_failing_control_an_experiment_with_a_missing_field_is_never_drawn_as_a_number() -> (
    None
):
    bad = copy.deepcopy(_history())
    for arm in bad["experiments"][0]["arms"]:
        arm["wilson_low"] = None
        arm["wilson_high"] = None
    for r in bad["series"]:
        for point in r["days"]:
            point["success"] = {"rate": None, "wilson_low": None, "wilson_high": None}
    drawn = _render(bad)["els"]
    # the fields the whiskers and the band need are gone; the page must not invent a number for them
    for view in ("models", "success", "experiments"):
        assert "NaN" not in drawn[view]
        assert "undefined" not in drawn[view]


def test_the_view_switch_shows_one_view_and_hides_the_others() -> None:
    first = _render(_history())["els"]["hidden"]
    assert first == {
        "models": False,
        "success": True,
        "experiments": True,
        "scenarios": True,
    }
    second = _render(_history(), clicks="experiments")["els"]["hidden"]
    assert second == {
        "models": True,
        "success": True,
        "experiments": False,
        "scenarios": True,
    }


def test_light_and_dark_and_phone_width_are_declared() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert "prefers-color-scheme:dark" in page
    assert 'name="viewport"' in page
    assert "@media(max-width:640px)" in page
    assert (
        re.findall(r'stroke="#|fill="#', page) == []
    )  # chart colours come from the page variables


# the section of the recorded reads -> the schema model class that carries those fields
SECTION_MODELS = {
    "top": schema.ModelDelegationHistory,
    "models": schema.ModelHistoryModel,
    "series": schema.ModelSeries,
    "points": schema.ModelSeriesPoint,
    "success": schema.ModelSuccess,
    "experiments": schema.ModelExperiment,
    "task_set": schema.ModelTaskSet,
    "baseline": schema.ModelBaseline,
    "arms": schema.ModelArm,
    "paired": schema.ModelPairedTest,
    "sources": schema.ModelHistorySource,
}


def _schema_fields() -> dict[str, set[str]]:
    return {name: set(cls.model_fields) for name, cls in SECTION_MODELS.items()}


def _unmatched(
    reads: dict[str, list[str]], schema_fields: dict[str, set[str]]
) -> dict[str, list[str]]:
    """The fields the page read that the schema model of their section does not carry, by section (empty when none)."""
    found = {
        section: sorted(set(names) - schema_fields.get(section, set()))
        for section, names in reads.items()
    }
    return {section: names for section, names in found.items() if names}


def test_every_field_the_page_reads_is_in_the_schema_model() -> None:
    reads = _render(_history())["reads"]
    assert set(reads) <= set(SECTION_MODELS), "a recorded section has no schema model"
    assert _unmatched(reads, _schema_fields()) == {}
    # the recorder sees the fields the review found missing from the old hand-kept list
    assert "window_days" in reads["top"]
    assert "n" in reads["arms"]


def test_failing_control_a_field_the_page_reads_but_the_schema_lacks_fails_the_check() -> (
    None
):
    """The check runs: with a field gone from the schema model's side, _unmatched names it (for a rename of window_days or arms.n too)."""
    reads = _render(_history())["reads"]
    for section, name in (
        ("models", "quantization"),
        ("top", "window_days"),
        ("arms", "n"),
    ):
        fields = _schema_fields()
        fields[section].discard(name)
        assert _unmatched(reads, fields) == {section: [name]}


def test_failing_control_a_history_without_window_days_is_not_drawn() -> None:
    doc = _history()
    del doc["window_days"]
    drawn = _render(doc)["els"]
    assert "no renderable shape, schema id or generated_at" in drawn["halert"]
    assert _numbers_drawn(drawn) == []


def _experiment_with_baseline(accuracy: float) -> dict:
    doc = copy.deepcopy(_history())
    exp = doc["experiments"][0]
    exp["majority_baseline"] = {
        "label": "majority class",
        "correct": round(accuracy * exp["n"]),
        "n": exp["n"],
        "accuracy": accuracy,
    }
    schema.ModelDelegationHistory.model_validate(doc)
    return doc


def test_the_majority_baseline_is_drawn_as_a_line_where_an_experiment_states_one() -> (
    None
):
    drawn = _render(_experiment_with_baseline(0.4))["els"]["experiments"]
    assert "majority baseline 40 %" in drawn
    assert ">baseline 40 %<" in drawn
    assert drawn.count('stroke-dasharray="4 3"') == 1


def test_control_without_a_baseline_the_page_says_so_and_draws_no_line() -> None:
    drawn = _render(_history())["els"]["experiments"]
    assert "no majority baseline stated" in drawn
    assert "stroke-dasharray" not in drawn
    assert "baseline " not in drawn.replace("no majority baseline stated", "")


def test_a_hosted_model_with_no_price_is_not_called_local() -> None:
    doc = _history()
    hosted = [m for m in doc["models"] if m["engine_kind"] != "local"]
    assert hosted
    for m in hosted:
        m["price_in_usd_per_mtok"] = None
        m["price_out_usd_per_mtok"] = None
    drawn = _render(doc)["els"]["models"]
    local = [m for m in doc["models"] if m["engine_kind"] == "local"]
    assert drawn.count("price not recorded") == len(hosted)
    assert drawn.count("no price (local)") == len(local)


def _with_models(n: int) -> dict:
    """The fixture with its first task kind's model cloned until n models answer it: a lab with more models than colours."""
    doc = copy.deepcopy(_history())
    base_row = next(r for r in doc["series"] if r["task_kind"] == "document")
    base = next(m for m in doc["models"] if m["id"] == base_row["model"])
    rows = [r for r in doc["series"] if r["model"] == base["id"]]
    i = 0
    while len(doc["models"]) < n:
        new_id = f"Model-Y{i}@192.0.2.{100 + i}"
        doc["models"].append({**base, "id": new_id, "host": f"192.0.2.{100 + i}"})
        doc["series"].extend({**copy.deepcopy(r), "model": new_id} for r in rows)
        i += 1
    doc["counts"]["models"] = len(doc["models"])
    doc["counts"]["series"] = len(doc["series"])
    schema.ModelDelegationHistory.model_validate(doc)
    return doc


LEGEND = re.compile(
    r'<span><svg[^>]*><line [^>]*stroke="(var\(--s\d\))" stroke-width="3"(?: stroke-dasharray="([^"]*)")?/></svg>([^<]*)</span>'
)


def test_no_two_models_of_one_chart_share_a_colour_and_a_line_style() -> None:
    drawn = _render(_with_models(18))["els"]["success"]
    cards = drawn.split('<section class="card">')[1:]
    document = next(c for c in cards if c.startswith("<h2>document"))
    legend = LEGEND.findall(document)
    assert len(legend) == 18
    assert len({(colour, dash) for colour, dash, _ in legend}) == 18
    assert len({name for *_, name in legend}) == 18
    # every drawn line of the card is one of the legend looks
    lines = set(
        re.findall(
            r'<polyline fill="none" stroke="(var\(--s\d\))" stroke-width="2"(?: stroke-dasharray="([^"]*)")? ',
            document,
        )
    )
    assert lines
    assert lines <= {(colour, dash) for colour, dash, _ in legend}


def test_control_the_legend_check_sees_a_shared_look() -> None:
    """With more looks wanted than the page has (24), a look repeats and the same uniqueness check fails."""
    drawn = _render(_with_models(26))["els"]["success"]
    document = next(
        c
        for c in drawn.split('<section class="card">')[1:]
        if c.startswith("<h2>document")
    )
    legend = LEGEND.findall(document)
    assert len(legend) == 26
    assert len({(colour, dash) for colour, dash, _ in legend}) == 24


def test_chart_text_is_readable_on_a_phone() -> None:
    page = PAGE.read_text(encoding="utf-8")
    font = float(re.search(r"\.chart\{[^}]*font:(\d+)px", page).group(1))
    drawn = _render(_with_models(4))["els"]
    widths = {
        int(w)
        for view in ("success", "experiments")
        for w in re.findall(r'<svg class="chart" viewBox="0 0 (\d+) ', drawn[view])
    }
    assert widths
    for width in widths:
        assert font * PHONE_CHART_PX / width >= MIN_TEXT_PX, (font, width)


def test_the_hash_names_the_view_when_it_changes_after_the_page_loaded() -> None:
    hidden = _render(_history(), hashchange="experiments")["els"]["hidden"]
    assert hidden == {
        "models": True,
        "success": True,
        "experiments": False,
        "scenarios": True,
    }


def test_a_scenario_pass_history_entry_is_escaped_in_the_strip_title() -> None:
    doc = json.loads(_tracker(red_scenario=False))
    doc["scenarios"][0]["recent"] = ["pass", "<b>x</b>"]
    drawn = _render(_history(), tracker_bytes=json.dumps(doc).encode())["els"]["scn"]
    assert "<title><b>x</b></title>" not in drawn
    assert "<title>&lt;b&gt;x&lt;/b&gt;</title>" in drawn
