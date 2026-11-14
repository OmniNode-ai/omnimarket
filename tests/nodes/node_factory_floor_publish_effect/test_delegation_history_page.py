# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Delegation tab draws four views from delegation-history.json: models, success over time, experiments, scenarios.

The chain: a history document (the fixture of the seam between the data lane and the page lane), the page's own script under
node, and the markup each view leaves. The failing controls are a history with one experiment arm missing a field the page
reads (the view must say so, never draw a number), an unreadable history (the views say no history and the scenarios still
draw), and a field the page reads that the schema does not have (the schema test fails).
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.nodes.node_factory_floor_publish_effect.test_compare_page import NODE_DIR
from tests.nodes.node_factory_floor_publish_effect.test_delegation_page import (
    _tracker,
)

pytestmark = pytest.mark.unit

PAGE = NODE_DIR / "page/delegation.html"
FIXTURE = Path(__file__).parent / "fixtures/delegation_history_page_fixture.json"
SCHEMA_ID = "omnimarket.floor_delegation_history.v1"
HISTORY_NOW = "2026-11-14T03:05:00+00:00"

HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, filesJson, nowIso] = process.argv.slice(1);
    const files = JSON.parse(filesJson);
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
    out = subprocess.run(
        [node, "-e", HARNESS, str(PAGE), json.dumps(files), now or HISTORY_NOW],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={"PAGE_CLICKS": clicks, "PATH": "/usr/bin:/bin"},
    )
    return json.loads(out.stdout)


def test_golden_every_view_is_drawn_from_the_fixture() -> None:
    doc = _history()
    drawn = _render(doc)["els"]
    assert drawn["halert"] == ""
    # models: one row per model, with engine, host, settings, price, the 14-day success and its interval
    for m in doc["models"]:
        assert m["model_id"] in drawn["models"]
    assert drawn["models"].count("<tr><td") == len(doc["models"])
    assert "no price (local)" in drawn["models"]
    assert "$0.600 in / $2.20 out per M tokens" in drawn["models"]
    assert "65536 ctx, Q4_K_M, thinking off" in drawn["models"]
    assert re.search(r"\d+ % to \d+ %, n=\d+", drawn["models"])
    assert f"{len(doc['models'])} models" in drawn["msum"]
    # success over time: one card per task kind, a polyline and a band per line, hover text carries n
    kinds = sorted({r["task_kind"] for r in doc["series"]})
    assert drawn["success"].count('<section class="card">') == len(kinds)
    assert "<polyline" in drawn["success"]
    assert "<polygon" in drawn["success"]
    assert re.search(
        r"<title>qwen3\.8-27b 2026-11-\d\d: \d+ % \(n=\d+\)</title>", drawn["success"]
    )
    assert f"{len(kinds)} task kinds" in drawn["ssumh"]
    # experiments: a card per experiment, newest first, the baseline line, the verdict and the p value
    exps = sorted(doc["experiments"], key=lambda e: e["date"], reverse=True)
    assert drawn["experiments"].count('class="card exp"') == len(exps)
    assert drawn["experiments"].index(exps[0]["question"]) < drawn["experiments"].index(
        exps[1]["question"]
    )
    assert "baseline 58 %" in drawn["experiments"]
    assert "baseline 31 %" in drawn["experiments"]
    for e in exps:
        assert e["verdict"] in drawn["experiments"]
    assert "p=0.00004" in drawn["experiments"] or "p=4.0e-5" in drawn["experiments"]
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


def test_failing_control_an_experiment_with_a_missing_field_is_never_drawn_as_a_number() -> (
    None
):
    doc = _history()
    bad = copy.deepcopy(doc)
    for arm in bad["experiments"][0]["arms"]:
        del arm["ci_lo"]
    drawn = _render(bad)["els"]
    # the field the whiskers need is gone; the page must not invent a number for it
    assert "NaN" not in drawn["experiments"]
    assert "undefined" not in drawn["experiments"]


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


def _schema_fields(doc: dict) -> dict[str, set[str]]:
    """Section -> the field names the schema carries, read from a document that conforms to it."""
    return {
        "top": set(doc),
        "models": set().union(*(set(m) for m in doc["models"])),
        "series": set().union(*(set(r) for r in doc["series"])),
        "experiments": set().union(*(set(e) for e in doc["experiments"])),
        "arms": set().union(*(set(a) for e in doc["experiments"] for a in e["arms"])),
    }


def test_every_field_the_page_reads_is_in_the_schema() -> None:
    reads = _render(_history())["reads"]
    schema = _schema_fields(_history())
    for section, names in reads.items():
        missing = sorted(set(names) - schema[section])
        assert not missing, (
            f"the page reads {missing} in {section}, which the schema does not carry"
        )


def test_failing_control_a_field_the_page_reads_but_the_schema_lacks_fails_the_check() -> (
    None
):
    doc = _history()
    for m in doc["models"]:
        del m["quantization"]
    reads = _render(_history())["reads"]
    schema = _schema_fields(doc)
    assert "quantization" in set(reads["models"]) - schema["models"]
