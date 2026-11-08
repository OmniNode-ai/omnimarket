# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared details panel and list (page/shared/floor-panel.js) behave as the operator asked (OMN-23018).

"Concise by default, inspectable on demand": a list shows 25 rows a page, a search box appears only when it holds more than 25
items, a page turns, a search narrows, and a panel that names a number says whether its lines add up to it. The script runs
under node with a small DOM; the served page was also driven in a browser and its readings are in the pull request.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

DASH = "\N{EN DASH}"
JS_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/shared/floor-panel.js"
)

HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [jsPath, casesJson] = process.argv.slice(1);
    const src = fs.readFileSync(jsPath, "utf8");
    const listeners = {};
    globalThis.window = {addEventListener() {}};
    globalThis.document = {addEventListener() {}, body: {append() {}}, createElement: () => ({})};
    globalThis.history = {state: null};
    globalThis.location = {hash: "", pathname: "/", search: ""};
    Date.now = () => Date.parse("2026-11-08T05:00:00Z");
    new Function("window", src)(globalThis.window);
    const FP = globalThis.window.FloorPanel;
    // A list host: records the innerHTML written to its body, the input handler and the click handler.
    function host() {
      const body = {innerHTML: ""}, q = {value: "", handlers: {}, addEventListener(k, f) { this.handlers[k] = f; }};
      const el = {
        classList: {add() {}}, innerHTML: "", handlers: {},
        removeEventListener(k, f) { this.handlers[k] = (this.handlers[k] || []).filter(x => x !== f); },
        querySelector(sel) { return sel === ".fp-body" ? body : sel === ".fp-q" ? (this.innerHTML.includes("fp-q") ? q : null) : null; },
        addEventListener(k, f) { (this.handlers[k] = this.handlers[k] || []).push(f); },
        getBoundingClientRect: () => ({top: 10}),
      };
      return {el, body, q};
    }
    const rowsOf = html => (html.match(/<li class="fp-it">/g) || []).length;
    const out = {};
    const cases = JSON.parse(casesJson);
    for (const c of cases) {
      const h = host();
      const info = FP.mount(h.el, c.items, {id: c.id});
      if (c.remount) { FP.mount(h.el, c.items, {id: c.id}); FP.mount(h.el, c.items, {id: c.id}); }
      const r = {shell: h.el.innerHTML, rows: rowsOf(h.body.innerHTML), html: h.body.innerHTML};
      const steps = [];
      for (const s of c.steps || []) {
        if (s.type) {
          h.q.value = s.type; h.q.handlers.input();
        } else if (s.page) {
          for (const f of h.el.handlers.click) f({target: {closest: () => ({disabled: false, getAttribute: () => String(s.page)})}});
        }
        steps.push({rows: rowsOf(h.body.innerHTML), html: h.body.innerHTML});
      }
      r.steps = steps;
      out[c.name] = r;
    }
    out.sum = {
      ok: FP.bodyHTML({key: "k", total: 4, sections: [{heading: "h", items: [{title: "a"}, {title: "b", count: 3}]}]}),
      bad: FP.bodyHTML({key: "k", total: 4, sections: [{heading: "h", items: [{title: "a"}, {title: "b", count: 2}]}]}),
      none: FP.bodyHTML({key: "k", sections: [{heading: "h", items: [{title: "a"}], empty: "x"}]}),
      empty: FP.bodyHTML({key: "k", sections: [{heading: "Nothing here", items: [], empty: "Nothing is listed."}]}),
    };
    console.log(JSON.stringify(out));
    """
)


def _items(n: int, prefix: str = "item") -> list[dict[str, Any]]:
    return [
        {
            "title": f"{prefix} {i}",
            "sub": "red" if i % 2 else "green",
            "group": f"g{i // 10}",
        }
        for i in range(n)
    ]


def _run(cases: list[dict[str, Any]]) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [node, "-e", HARNESS, str(JS_PATH), json.dumps(cases)],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode()
    out = json.loads(done.stdout.decode().strip().splitlines()[-1])
    assert isinstance(out, dict)
    return out


def test_golden_a_list_of_25_or_fewer_shows_every_row_with_no_search_and_no_pager() -> (
    None
):
    got = _run([{"name": "small", "id": "a", "items": _items(25)}])["small"]
    assert got["rows"] == 25
    assert "fp-q" not in got["shell"]
    assert "fp-pg" not in got["html"]


def test_golden_a_longer_list_gets_a_search_box_and_pages_of_25() -> None:
    got = _run(
        [
            {
                "name": "long",
                "id": "b",
                "items": _items(60),
                "steps": [{"page": 1}, {"page": 1}],
            }
        ]
    )["long"]
    assert "fp-q" in got["shell"]
    assert "Search 60 lines" in got["shell"]
    assert got["rows"] == 25
    assert f"1{DASH}25 of 60" in got["html"]
    page2, page3 = got["steps"]
    assert page2["rows"] == 25
    assert f"26{DASH}50 of 60" in page2["html"]
    assert page3["rows"] == 10
    assert f"51{DASH}60 of 60" in page3["html"]
    assert 'data-pg="1" disabled' in page3["html"]


def test_error_mounting_again_into_the_same_element_does_not_stack_page_handlers() -> (
    None
):
    # A page that redraws into one element (the Better or worse page) mounts on every refresh: one tap is one page.
    got = _run(
        [
            {
                "name": "again",
                "id": "r",
                "items": _items(60),
                "remount": True,
                "steps": [{"page": 1}],
            }
        ]
    )["again"]
    assert f"26{DASH}50 of 60" in got["steps"][0]["html"]


def test_golden_a_search_narrows_the_list_and_returns_to_the_first_page() -> None:
    got = _run(
        [
            {
                "name": "find",
                "id": "c",
                "items": _items(60),
                "steps": [{"page": 1}, {"type": "item 5"}, {"type": "no such thing"}],
            }
        ]
    )["find"]
    _, narrowed, none = got["steps"]
    # "item 5", "item 50" .. "item 59": eleven lines, one page.
    assert narrowed["rows"] == 11
    assert "11 of 60 match" in narrowed["html"]
    assert none["rows"] == 0
    assert "No line matches" in none["html"]


def test_golden_a_group_heading_is_drawn_once_where_the_group_starts() -> None:
    got = _run([{"name": "groups", "id": "d", "items": _items(25)}])["groups"]
    assert got["html"].count('class="fp-grp"') == 3
    assert got["html"].index("g0") < got["html"].index("g1") < got["html"].index("g2")


def test_error_a_link_is_drawn_only_for_an_http_address_and_text_is_escaped() -> None:
    items = [
        {"title": "<b>x</b>", "href": "javascript:alert(1)"},
        {"title": "ok", "href": "https://example.test/a?b=1&c=2"},
    ]
    got = _run([{"name": "links", "id": "e", "items": items}])["links"]
    assert "javascript:" not in got["html"]
    assert "&lt;b&gt;x&lt;/b&gt;" in got["html"]
    assert 'href="https://example.test/a?b=1&amp;c=2"' in got["html"]
    assert 'rel="noopener noreferrer"' in got["html"]


def test_golden_a_panel_that_names_a_number_says_its_lines_add_up_to_it() -> None:
    sums = _run([])["sum"]
    assert 'data-fp-sum="ok"' in sums["ok"]
    assert "add up to the number: 4" in sums["ok"]


def test_failing_control_lines_that_do_not_add_up_are_called_out_in_red() -> None:
    sums = _run([])["sum"]
    assert 'data-fp-sum="bad"' in sums["bad"]
    assert "add up to 3, not the number 4" in sums["bad"]
    assert "data-fp-sum" not in sums["none"]


def test_golden_an_empty_section_says_why_instead_of_drawing_nothing() -> None:
    sums = _run([])["sum"]
    assert "Nothing is listed." in sums["empty"]
    assert "Nothing here" in sums["empty"]
