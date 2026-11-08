# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The main page (the simple floor) has a tab bar, Process and Machines, and the Machines tab draws every host (OMN-22478).

The operator, 2026-11-06 about 18:30Z, on the main page at ``#process``: "this also shows no machine tab". The Machines view
was only on the Details page behind a link. These tests run the main page's own script under node against two records
served by the floor (the simple floor record and the factory floor record, whose ``floor.host_status`` is the per-host
model), so the chain is the served record to the drawn card. The failing control is the same page with the Machines tab
taken out: it draws no card.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.tests.test_machines_tab import (
    _machines,
    _record,
)
from tests.nodes.node_factory_floor_compute.test_host_status import (
    full_facts,
    stale_facts,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).resolve().parents[1]
SIMPLE_PATH = NODE_DIR / "page/simple.html"
SIMPLE_SRC = SIMPLE_PATH.read_text(encoding="utf-8")
FIXTURES = Path(__file__).parent / "fixtures"
HOSTS = json.loads(
    (FIXTURES / "factory_floor_host_status_served_2026-11-06T1835Z.json").read_text(
        encoding="utf-8"
    )
)
SIMPLE = json.loads(
    (FIXTURES / "floor_simple_served_2026-11-06T1838Z.json").read_text(encoding="utf-8")
)
HOST_LABELS = [
    "h051",
    "h053",
    "h101",
    "h105",
    "h106",
    "h200 (the Mac)",
    "h201 (server)",
    "h202",
    "h203",
]

# Runs a page's script with a small DOM, applies each hash in turn (firing the page's hashchange listeners) and reports
# which tab is selected, which pane is shown and what each container drew after each step.
HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, filesJson, optsJson] = process.argv.slice(1);
    const files = JSON.parse(filesJson), opts = JSON.parse(optsJson);
    const src = fs.readFileSync(pagePath, "utf8");
    const script = /<script>([\\s\\S]*?)<\\/script>/.exec(src)[1];
    const els = {}, listeners = {};
    const mk = id => ({id, innerHTML: "", textContent: "", hidden: false, attrs: {}, style: {}, dataset: {},
      setAttribute(k, v) { this.attrs[k] = v; }, addEventListener() {}});
    globalThis.document = {title: "", getElementById: id => els[id] || (els[id] = mk(id)), addEventListener() {},
      createElement: () => mk("created"), body: {append() {}}, querySelectorAll: () => [], activeElement: null};
    // The shared panel (floor-panel.js) has its own tests; the page's script only configures and refreshes it.
    globalThis.FloorPanel = {configure() {}, sync() {}, update() {}, isOpen: () => false, restoring: false};
    globalThis.window = {addEventListener(k, f) { (listeners[k] = listeners[k] || []).push(f); }};
    globalThis.location = {hash: opts.hash || ""};
    const now = Date.parse(opts.now);
    Date.now = () => now;
    globalThis.setInterval = () => 0;
    globalThis.fetch = async url => (url in files
      ? {ok: true, status: 200, json: async () => JSON.parse(files[url])}
      : {ok: false, status: 404, json: async () => ({})});
    const state = () => ({
      selected: Object.keys(els).filter(k => k.startsWith("tab-") && els[k].attrs["aria-selected"] === "true"),
      shown: Object.keys(els).filter(k => k.startsWith("pane-") && !els[k].hidden),
    });
    (async () => {
      new Function(script).call(globalThis);
      await new Promise(r => setTimeout(r, 150));
      const steps = [state()];
      for (const h of opts.hashes || []) {
        globalThis.location.hash = h;
        for (const f of listeners.hashchange || []) f();
        steps.push(state());
      }
      console.log(JSON.stringify({steps, machines: els.machines ? els.machines.innerHTML : "", app: els.app ? els.app.innerHTML : ""}));
    })();
    """
)


def _run(
    files: dict[str, Any],
    *,
    src: str = SIMPLE_SRC,
    now: str = "2026-11-06T18:40:00Z",
    hash_: str = "",
    hashes: list[str] | None = None,
) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(src, encoding="utf-8")
        done = subprocess.run(
            [
                node,
                "-e",
                HARNESS,
                str(page),
                json.dumps({k: json.dumps(v) for k, v in files.items()}),
                json.dumps({"now": now, "hash": hash_, "hashes": hashes or []}),
            ],
            capture_output=True,
            check=False,
            timeout=60,
        )
    assert done.returncode == 0, done.stderr.decode()
    out = json.loads(done.stdout.decode().strip().splitlines()[-1])
    assert isinstance(out, dict)
    return out


def _served(**host_record: Any) -> dict[str, Any]:
    return {
        "floor-simple.json": SIMPLE,
        "factory-floor.json": {**HOSTS, **host_record},
    }


def _labels(html: str) -> list[str]:
    return re.findall(r'<article class="hcard [^"]*" aria-label="([^"]+)"', html)


def _cards(html: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in re.split(r'(?=<article class="hcard)', html)[1:]:
        match = re.search(r'aria-label="([^"]+)"', part)
        assert match is not None
        found[match.group(1)] = part
    return found


def test_the_main_page_declares_the_process_and_machines_tabs_as_deep_links() -> None:
    assert re.findall(r'<a role="tab" id="tab-(\w+)" href="#(\w+)"', SIMPLE_SRC) == [
        ("process", "process"),
        ("machines", "machines"),
    ]
    assert 'role="tablist"' in SIMPLE_SRC
    assert 'id="pane-process"' in SIMPLE_SRC
    assert 'id="pane-machines"' in SIMPLE_SRC


def test_golden_the_machines_tab_draws_the_nine_hosts_of_the_served_record() -> None:
    out = _run(_served(), hash_="#machines")
    assert _labels(out["machines"]) == HOST_LABELS
    assert "9 hosts in the host table" in out["machines"]
    assert "Record read 4 min ago" in out["machines"]
    cards = _cards(out["machines"])
    assert "Refusing new work" in cards["h053"]
    assert "Read" in cards["h101"]
    assert "Out of selection" in cards["h051"]
    assert "NVMe drive fit" in cards["h051"]


def test_golden_every_unread_cell_names_its_reason_and_is_never_a_number() -> None:
    out = _run(_served(), hash_="#machines")
    unread = re.findall(
        r'<span class="v unread"[^>]*>unread \(([^)]+)\)</span>', out["machines"]
    )
    assert unread, "the served record carries unread cells"
    assert all(reason.strip() for reason in unread)


def test_golden_the_deep_links_select_the_tab_and_pane_without_a_reload() -> None:
    out = _run(
        _served(), hashes=["#machines", "#process", "#nonsense", "#machines", ""]
    )
    process = {"selected": ["tab-process"], "shown": ["pane-process"]}
    machines = {"selected": ["tab-machines"], "shown": ["pane-machines"]}
    assert out["steps"] == [
        process,  # no hash: the process view, as the page always was
        machines,
        process,
        process,  # an unknown hash falls back to the process view
        machines,
        process,
    ]


def test_golden_each_deep_link_opens_its_tab_on_load() -> None:
    for hash_, pane in (("#process", "pane-process"), ("#machines", "pane-machines")):
        out = _run(_served(), hash_=hash_)
        assert out["steps"][0]["shown"] == [pane]


def test_golden_the_process_view_still_draws_its_sentence() -> None:
    out = _run(_served(), hash_="#process")
    assert SIMPLE["sentence"] in out["app"]
    assert "Details: lines, machines, shipping, delegation" in out["app"]


def test_error_a_record_that_cannot_be_read_says_so_and_draws_no_card() -> None:
    out = _run({"floor-simple.json": SIMPLE}, hash_="#machines")
    assert "The machine record cannot be read (HTTP 404)" in out["machines"]
    assert _labels(out["machines"]) == []


def test_error_a_record_without_per_host_status_is_drawn_as_unread_not_as_empty_hosts() -> (
    None
):
    out = _run(_served(floor={"host_status": []}), hash_="#machines")
    assert "The record carries no per-host status" in out["machines"]
    assert _labels(out["machines"]) == []


def test_error_an_old_record_is_marked_old_and_still_draws_its_cards() -> None:
    out = _run(_served(), now="2026-11-06T21:00:00Z", hash_="#machines")
    assert "The machine record was read 2 h ago, the limit is 15 min" in out["machines"]
    assert len(_labels(out["machines"])) == 9


def test_error_a_stale_reading_is_marked_stale_from_a_recorded_pool_read() -> None:
    record = _record(**stale_facts())
    out = _run(
        {"floor-simple.json": SIMPLE, "factory-floor.json": record},
        now=record["generated_at"],
        hash_="#machines",
    )
    assert 'class="chip stall"' in out["machines"]
    assert "Reading out of date" in out["machines"]


def test_golden_the_card_is_the_one_the_details_page_draws() -> None:
    """Both pages draw floor.host_status from the compute node's own record; the cards must not drift apart."""
    record = _record(**full_facts())
    main = _run(
        {"floor-simple.json": SIMPLE, "factory-floor.json": record},
        now=record["generated_at"],
        hash_="#machines",
    )["machines"]
    detail = _machines(record)

    def cards(html: str) -> list[str]:
        return [
            re.sub(r"\s+", " ", c.replace("&#39;", "'")).split("</article>")[0]
            for c in re.split(r'(?=<article class="hcard)', html)[1:]
        ]

    assert len(cards(main)) == len(record["floor"]["host_status"]) == 5
    assert cards(main) == cards(detail)


def test_failing_control_the_page_without_the_machines_tab_draws_no_card() -> None:
    """The same records through the page with the tab taken out draw nothing, so the assertions above can fail."""
    broken = SIMPLE_SRC.replace("load(); loadMachines();", "load();")
    assert broken != SIMPLE_SRC
    out = _run(_served(), src=broken, hash_="#machines")
    assert _labels(out["machines"]) == []
    assert "9 hosts in the host table" not in out["machines"]
