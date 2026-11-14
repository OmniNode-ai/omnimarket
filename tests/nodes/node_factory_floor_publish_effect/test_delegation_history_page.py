# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Delegation tab draws four views from delegation-history.json: models, success over time, experiments, scenarios.

The chain: the history document the data lane's fold produces (the fixture of the seam between the data lane and the page lane,
read through the data lane's own schema model), the page's own script under node, and the markup each view leaves. The failing
controls are a history with one experiment arm missing a field the page reads (the view must say so, never draw a number), an
unreadable history (the views say no history and the scenarios still draw), and a field the page reads that the schema model does
not have (the schema test fails).
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from omnimarket.nodes.node_floor_delegation_history_compute.models import (
    model_delegation_history as schema,
)
from tests.nodes.node_factory_floor_publish_effect.test_compare_page import NODE_DIR
from tests.nodes.node_factory_floor_publish_effect.test_delegation_page import (
    _tracker,
)

pytestmark = pytest.mark.unit

PAGE = NODE_DIR / "page/delegation.html"
FIXTURE = (
    Path(__file__).parents[1]
    / "node_floor_delegation_history_compute/fixtures/delegation-history.fixture.json"
)
HISTORY_NOW = "2026-11-14T03:00:00+00:00"

HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, filesPath, nowIso] = process.argv.slice(1);
    const files = JSON.parse(fs.readFileSync(filesPath, "utf8"));
    const src = fs.readFileSync(pagePath, "utf8");
    const script = /<script>([\\s\\S]*?)<\\/script>/.exec(src)[1];
    const els = {};
    const mk = id => ({id, innerHTML: "", textContent: "", value: "", dataset: {}, hidden: false, open: false, setAttribute() {},
      removeAttribute() {}, addEventListener() {}, querySelectorAll() { return []; }, showModal() {}, close() {}, focus() {}});
    const handlers = {};
    globalThis.document = {title: "", getElementById: id => els[id] || (els[id] = mk(id)), addEventListener: (t, fn) => { handlers[t] = fn; }};
    globalThis.window = {location: {hash: process.env.PAGE_HASH || ""}};
    const now = Date.parse(nowIso);
    Date.now = () => now;
    globalThis.setInterval = () => 0;
    globalThis.fetch = async url => (url in files
      ? {ok: true, status: 200, json: async () => JSON.parse(files[url])}
      : {ok: false, status: 404, json: async () => ({})});
    (async () => {
      new Function(script + "\\n;globalThis.__READS = READS;")();
      await new Promise(r => setTimeout(r, 50));
      for (const v of (process.env.PAGE_CLICKS || "").split(",").filter(Boolean)) handlers.click({target: {closest: () => ({dataset: {view: v}})}});
      const out = {};
      for (const id of ["halert", "models", "msum", "success", "ssumh", "experiments", "esum", "scn", "v-models", "v-success", "v-experiments", "v-scenarios"]) out[id] = els[id] ? (els[id].innerHTML || els[id].textContent) : "";
      out.hidden = Object.fromEntries(["models", "success", "experiments", "scenarios"].map(v => [v, !!(els["v-" + v] && els["v-" + v].hidden)]));
      process.stdout.write(JSON.stringify({els: out, reads: globalThis.__READS}));
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
) -> dict:
    node = shutil.which("node")
    assert node, "the delegation history page render test requires Node.js"
    files: dict[str, str] = {}
    if tracker:
        files["delegation-tracker.json"] = _tracker(red_scenario=False).decode()
    if history is not None:
        files["delegation-history.json"] = json.dumps(history)
    # the history document is large: it goes to the script as a file, never as an argument
    with tempfile.TemporaryDirectory() as tmp:
        files_path = Path(tmp) / "files.json"
        files_path.write_text(json.dumps(files), encoding="utf-8")
        out = subprocess.run(
            [node, "-e", HARNESS, str(PAGE), str(files_path), now or HISTORY_NOW],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
            env={"PAGE_CLICKS": clicks, "PATH": "/usr/bin:/bin"},
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


def test_error_a_stale_history_is_marked_old() -> None:
    doc = _history()
    doc["generated_at"] = "2026-11-10T00:00:00Z"
    assert "old, the limit is" in _render(doc)["els"]["halert"]


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


# the section of READS -> the data lane's model class that carries those fields
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


def test_every_field_the_page_reads_is_in_the_schema_model() -> None:
    reads = _render(_history())["reads"]
    assert set(reads) <= set(SECTION_MODELS), "a READS section has no schema model"
    schema_fields = _schema_fields()
    for section, names in reads.items():
        missing = sorted(set(names) - schema_fields[section])
        assert not missing, (
            f"the page reads {missing} in {section}, which the schema model does not carry"
        )


def test_failing_control_a_field_the_page_reads_but_the_schema_lacks_fails_the_check() -> (
    None
):
    reads = _render(_history())["reads"]
    schema_fields = _schema_fields()
    schema_fields["models"].discard("quantization")
    assert "quantization" in set(reads["models"]) - schema_fields["models"]
