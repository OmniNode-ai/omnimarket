# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor page draws the Limits panel from the record's ``limits`` section."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

PAGE = (
    Path(__file__).resolve().parents[2]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/floor/index.html"
)

HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, recordJson] = process.argv.slice(1);
    const script = /<script>([\\s\\S]*?)<\\/script>/.exec(fs.readFileSync(pagePath, "utf8"))[1];
    const els = {};
    const mk = id => ({id, innerHTML: "", textContent: "", hidden: false, dataset: {}, style: {}, className: "",
      tabIndex: 0, title: "", children: [], classList: {add() {}, remove() {}, toggle() {}},
      querySelectorAll: () => [], setAttribute() {}, addEventListener() {}, replaceChildren() {},
      appendChild(c) { this.children.push(c); }, focus() {}});
    globalThis.document = {title: "", body: mk("body"), documentElement: mk("html"),
      getElementById: id => els[id] || (els[id] = mk(id)), addEventListener() {},
      querySelectorAll: () => [], querySelector: () => null, createElement: () => mk("created")};
    globalThis.window = {addEventListener() {}, matchMedia: () => ({matches: false, addEventListener() {}})};
    globalThis.location = {hash: "", search: ""};
    globalThis.history = {replaceState() {}};
    globalThis.localStorage = {getItem: () => null, setItem() {}};
    globalThis.getComputedStyle = () => ({getPropertyValue: () => "1"});
    globalThis.setInterval = () => 0;
    globalThis.fetch = async url => ({ok: true, status: 200, json: async () => JSON.parse(recordJson)});
    (async () => {
      new Function(script).call(globalThis);
      await new Promise(r => setTimeout(r, 150));
      console.log(JSON.stringify({limits: (els["limits"] || {}).innerHTML || "", hidden: (els["limits"] || {}).hidden}));
    })();
    """
)

LIMITS: dict[str, Any] = {
    "publish_cycle_minutes": 5,
    "unread_sources": [],
    "idle_slot_hours": 12.5,
    "idle_slot_hours_read_at": "2026-10-31T11:59:00Z",
    "rows": [
        {
            "row_id": "api_credit:k1",
            "kind": "api_credit",
            "name": "k1",
            "state": "read",
            "source": "api-credit-meter",
            "read_at": "2026-10-31T11:59:00Z",
            "unknown_reason": "",
            "unknown_fields": [],
            "values": {
                "month_spend_usd": 61.0,
                "budget_usd": 200.0,
                "remaining_usd": 139.0,
            },
        },
        {
            "row_id": "codex:codex",
            "kind": "codex",
            "name": "codex",
            "state": "UNKNOWN",
            "source": "codex usage",
            "read_at": "",
            "unknown_reason": "never read",
            "unknown_fields": [],
            "values": {},
        },
    ],
}


def _render(limits: dict[str, Any]) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    record = {
        "schema_id": "factory-floor/1",
        "generated_at": "2026-10-31T12:00:00Z",
        "lines": [],
        "floor": {
            "machines": [],
            "lanes": [],
            "bays": [],
            "shipped": [],
            "waiting": [],
            "stoppages": [],
            "cloud": None,
        },
        "sources": {},
        "limits": limits,
    }
    done = subprocess.run(
        [node, "-e", HARNESS, str(PAGE), json.dumps(record)],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode()
    out: dict[str, Any] = json.loads(done.stdout.decode().strip().splitlines()[-1])
    return out


def test_page_limits_panel_has_its_section_in_the_page() -> None:
    html = PAGE.read_text(encoding="utf-8")
    assert 'id="limits"' in html
    assert "renderLimits" in html


def test_page_limits_panel_draws_a_row_per_limit_with_source_and_read_time() -> None:
    out = _render(LIMITS)
    html = out["limits"]
    assert out["hidden"] is False
    assert "api-credit-meter" in html
    assert "k1" in html
    assert "139" in html
    assert "codex usage" in html


def test_page_limits_panel_draws_unknown_for_an_unread_row_and_never_a_zero() -> None:
    html = _render(LIMITS)["limits"]
    assert "UNKNOWN" in html
    assert "never read" in html


def test_page_limits_panel_shows_the_days_idle_slot_hours_beside_per_line_idle() -> (
    None
):
    html = _render(LIMITS)["limits"]
    assert "idle slot-hours" in html.lower()
    assert "12.5" in html


def test_page_limits_panel_says_unknown_when_the_idle_slot_hours_were_not_read() -> (
    None
):
    html = _render({**LIMITS, "idle_slot_hours": None, "idle_slot_hours_read_at": ""})[
        "limits"
    ]
    lowered = html.lower()
    assert "idle slot-hours" in lowered
    assert "UNKNOWN" in html
