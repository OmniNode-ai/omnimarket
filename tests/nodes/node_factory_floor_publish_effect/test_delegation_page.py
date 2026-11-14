# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23081: the Delegation tab is served by the floor's publisher and draws the shippable line, the counts and the tickets.

The chain: the tracker built from planted readings, the publisher's copy to the served directory, an HTTP read of
``delegation.html`` and ``delegation-tracker.json``, and the page's own script under node drawing the served tracker. The
failing control is a tracker with one failing scenario, which the page must draw as not shippable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    load_runtime,
)
from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_tracker import (
    build_tracker,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_delegation_routes import (
    ModelRouteHour,
    ModelRoutesBlock,
)
from tests.nodes.node_factory_floor_publish_effect.test_compare_page import (
    NODE_DIR,
    _publish_and_read,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    _home,
)
from tests.nodes.node_floor_delegation_effect.support import (
    NOW,
    RT,
    all_reads,
    inputs,
    lane,
    mirror,
    pr,
    read,
    run,
)

pytestmark = pytest.mark.unit

PAGE = NODE_DIR / "page/delegation.html"
TOKEN = b"__HISTORY_MAX_AGE_S__"


def published_page() -> bytes:
    """The page as the publisher serves it: the history age limit token replaced from the node contract's runtime block."""
    return PAGE.read_bytes().replace(
        TOKEN, str(load_runtime().delegation_history_max_age_seconds).encode()
    )


def _tracker(*, red_scenario: bool) -> bytes:
    reads = list(all_reads("success"))
    if red_scenario:
        reads[6] = read("R7", run("failure", 10))
    reads[1] = read("R2", kind="no_workflow", reason="HTTP 404")
    doc = build_tracker(
        RT,
        inputs(
            reads=tuple(reads),
            mirrors=(
                mirror(23161, status_type="completed"),
                mirror(23164, start_when="A3c ([OMN-23162](x)) has merged"),
            ),
            prs=(
                pr(23162, "merged", number=1),
                pr(23163, "open", "red", number=2),
                pr(23165, "open", "green", number=3),
            ),
            lanes=(lane(23166, "h053"),),
        ),
        NOW,
    )
    return doc.model_dump_json().encode()


def test_the_publisher_serves_the_delegation_page_and_the_tracker(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    target = home / RT.output_relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_tracker(red_scenario=True))
    served = _publish_and_read(
        tmp_path, ("delegation.html", "delegation-tracker.json", "publish-status.json")
    )
    assert served["delegation.html"] == published_page()
    assert TOKEN not in served["delegation.html"]
    doc = json.loads(served["delegation-tracker.json"])
    assert doc["shippable"] is False
    assert doc["blocking"] == ["R2", "R7"]
    assert (
        json.loads(served["publish-status.json"])["delegation_tracker"] == "published"
    )


def test_error_no_tracker_file_means_no_data_is_published_and_the_status_says_absent(
    tmp_path: Path,
) -> None:
    _home(tmp_path)
    served = _publish_and_read(
        tmp_path, ("delegation.html", "delegation-tracker.json", "publish-status.json")
    )
    assert served["delegation.html"]  # the page is served, and draws its own failure
    assert served["delegation-tracker.json"] == b""
    assert json.loads(served["publish-status.json"])["delegation_tracker"] == "absent"


HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, filesJson, nowIso, clicksJson] = process.argv.slice(1);
    const files = JSON.parse(filesJson), clicks = JSON.parse(clicksJson);
    const src = fs.readFileSync(pagePath, "utf8");
    const script = /<script>([\\s\\S]*?)<\\/script>/.exec(src)[1];
    const els = {};
    const mk = id => ({id, innerHTML: "", textContent: "", value: "", dataset: {}, open: false, setAttribute() {}, removeAttribute() {},
      addEventListener() {}, showModal() { this.open = true; }, close() { this.open = false; }, focus() {}});
    const handlers = {};
    globalThis.document = {title: "", getElementById: id => els[id] || (els[id] = mk(id)),
      addEventListener: (type, fn) => { handlers[type] = fn; }};
    globalThis.window = {};
    const now = Date.parse(nowIso);
    Date.now = () => now;
    globalThis.setInterval = () => 0;
    globalThis.fetch = async url => (url in files
      ? {ok: true, status: 200, json: async () => JSON.parse(files[url])}
      : {ok: false, status: 404, json: async () => ({})});
    (async () => {
      new Function(script)();
      await new Promise(r => setTimeout(r, 50));
      const panels = {};
      for (const key of clicks) {
        handlers.click({target: {closest: () => ({dataset: {open: key}})}});
        panels[key] = {title: els["dlg-title"].textContent, kick: els["dlg-kick"].textContent, body: els["dlg-body"].innerHTML};
      }
      const out = {};
      for (const id of ["alert", "ship", "cards", "routes", "rsum", "scn", "tk", "pg", "src", "dsum", "ssum", "tsum"]) out[id] = els[id] ? (els[id].innerHTML || els[id].textContent) : "";
      process.stdout.write(JSON.stringify({els: out, title: document.title, panels}));
    })();
    """
)


def _render(doc: bytes | None, clicks: tuple[str, ...] = ()) -> dict:
    node = shutil.which("node")
    assert node, "the delegation page render test requires Node.js"
    files = {} if doc is None else {"delegation-tracker.json": doc.decode()}
    with tempfile.TemporaryDirectory() as tmp:
        served = Path(tmp) / "delegation.html"
        served.write_bytes(published_page())
        out = subprocess.run(
            [
                node,
                "-e",
                HARNESS,
                str(served),
                json.dumps(files),
                NOW.isoformat(),
                json.dumps(list(clicks)),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    return json.loads(out.stdout)


def test_failing_control_one_red_scenario_is_drawn_not_shippable() -> None:
    drawn = _render(_tracker(red_scenario=True))
    assert "Shippable: no" in drawn["els"]["ship"]
    assert "Shippable: yes" not in drawn["els"]["ship"]
    assert "(not shippable)" in drawn["title"]
    assert "R7" in drawn["els"]["scn"]
    assert "Failed (failure)" in drawn["els"]["scn"]


def test_all_scenarios_passing_is_drawn_shippable() -> None:
    doc = build_tracker(RT, inputs(reads=all_reads("success")), NOW)
    drawn = _render(doc.model_dump_json().encode())
    assert "Shippable: yes" in drawn["els"]["ship"]
    assert "(shippable)" in drawn["title"]


def test_the_first_screen_shows_counts_by_group_and_every_scenario_and_ticket() -> None:
    drawn = _render(_tracker(red_scenario=True))
    cards = drawn["els"]["cards"]
    for words in (
        "7 of 9",
        "passing",
        "not built",
        "done",
        "merged, awaiting the closer",
        "pull request open",
        "lane running",
        "not started",
        "1 red",
        "1 green",
        "can start",
        "waiting",
    ):
        assert words in cards, words
    assert drawn["els"]["scn"].count('data-open="scn:') == 9
    assert drawn["els"]["tk"].count('data-open="tk:') == 23


def test_every_number_and_row_opens_its_details() -> None:
    drawn = _render(
        _tracker(red_scenario=True),
        (
            "ship",
            "scn:R7",
            "tk:OMN-23163",
            "grp:pr_open",
            "pr:red",
            "scs:no_workflow",
            "start:true",
            "src:watcher",
        ),
    )
    panels = drawn["panels"]
    assert "Not passing" in panels["ship"]["body"]
    assert "Failed (failure)" in panels["scn:R7"]["kick"]
    assert "open the run" in panels["scn:R7"]["body"]
    ticket = panels["tk:OMN-23163"]
    assert "Pull request open" in ticket["kick"]
    assert "omnimarket#2" in ticket["body"]
    assert "OMN-23163" in panels["grp:pr_open"]["body"]
    assert "OMN-23165" in panels["grp:pr_open"]["body"]
    assert "OMN-23163" in panels["pr:red"]["body"]
    assert "OMN-23165" not in panels["pr:red"]["body"]
    assert "R2" in panels["scs:no_workflow"]["body"]
    assert "OMN-23164" in panels["start:true"]["body"]
    assert panels["src:watcher"]["title"] == "Pull request watcher"


def test_a_missing_tracker_draws_a_failure_not_an_empty_page() -> None:
    drawn = _render(None)
    assert "cannot be read" in drawn["els"]["alert"]


def test_an_old_tracker_is_drawn_stale_in_the_title_and_the_alert() -> None:
    doc = json.loads(_tracker(red_scenario=False))
    doc["generated_at"] = "2026-11-08T10:00:00Z"
    drawn = _render(json.dumps(doc).encode())
    assert drawn["title"].startswith("STALE ")
    assert "old, the limit is" in drawn["els"]["alert"]


def test_a_source_that_could_not_be_read_is_named_on_the_page() -> None:
    doc = build_tracker(RT, inputs(reads=all_reads(), watcher=False), NOW)
    drawn = _render(doc.model_dump_json().encode())
    assert "Pull request watcher could not be read fresh" in drawn["els"]["alert"]
    assert "not readable" in drawn["els"]["cards"]


def _with_routes(block: ModelRoutesBlock) -> bytes:
    base = inputs(reads=all_reads("success"))
    return (
        build_tracker(RT, base.model_copy(update={"routes": block}), NOW)
        .model_dump_json()
        .encode()
    )


def test_a_fresh_routes_block_is_drawn_per_hour_with_the_local_share() -> None:
    """OMN-23381 AC3: the hour table shows local, GLM, other and the local share of the last hour (5 of 133)."""
    last = ModelRouteHour(
        hour="2026-11-08T20:00:00Z",
        local=5,
        glm=128,
        other=0,
        total=133,
        local_share=5 / 133,
    )
    block = ModelRoutesBlock(
        state="fresh",
        read_at="2026-11-08T20:00:00Z",
        age_minutes=0,
        max_age_min=90,
        window_hours=6,
        hours=(last,),
        last_hour=last,
        local_share=last.local_share,
    )
    drawn = _render(_with_routes(block))
    assert "fresh" in drawn["els"]["rsum"]
    table = drawn["els"]["routes"]
    for words in ("<td>5</td>", "<td>128</td>", "<td>133</td>", "3.8 %", "Local share"):
        assert words in table, words


def test_failing_control_routes_that_are_not_fresh_are_drawn_with_no_number_and_no_share() -> (
    None
):
    for state in ("stale", "unread"):
        block = ModelRoutesBlock(
            state=state,
            reason="the read is old or missing",
            read_at="2026-11-08T10:00:00Z",
            max_age_min=90,
        )
        drawn = _render(_with_routes(block))
        assert state in drawn["els"]["rsum"]
        assert "No number is drawn, and no share" in drawn["els"]["routes"]
        assert "<td>" not in drawn["els"]["routes"]
