# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The data-health strip: one block on the Dashboard and the Floor, states readable without colour, never live when stale."""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import textwrap
from typing import Any, cast

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    NODE_DIR,
)

pytestmark = pytest.mark.unit

DASHBOARD = NODE_DIR / "page/index.html"
FLOOR = NODE_DIR / "page/floor/index.html"
BLOCK = re.compile(r"// BEGIN health-strip\n.*?// END health-strip", re.S)
CSS_BLOCK = re.compile(
    r"/\* BEGIN health-strip-css \*/.*?/\* END health-strip-css \*/", re.S
)

# Runs the strip block against a stand-in document. Each step sets the browser clock, draws a record or a failed fetch,
# and reports what the strip showed and how many times the live region was written.
HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, stepsJson] = process.argv.slice(1);
    const src = fs.readFileSync(pagePath, "utf8");
    const block = /\\/\\/ BEGIN health-strip\\n[\\s\\S]*?\\/\\/ END health-strip/.exec(src)[0];
    const steps = JSON.parse(stepsJson);
    const els = {};
    const mk = id => {
      const e = {id, innerHTML: "", hidden: false, writes: 0, _text: ""};
      Object.defineProperty(e, "textContent", {get() { return this._text; }, set(v) { this._text = v; this.writes += 1; }});
      return e;
    };
    const el = id => els[id] || (els[id] = mk(id));
    const esc = s => String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    globalThis.location = {search: ""};
    let clock = 0;
    Date.now = () => clock;
    const api = new Function("el", "esc", block + "\\nreturn {drawHealth};")(el, esc);
    const out = [];
    for (const step of steps) {
      clock = Date.parse(step.now);
      api.drawHealth(step.record || null, step.failure || null);
      out.push({html: el("hstrip").innerHTML, live: el("hstrip-live").textContent, liveWrites: el("hstrip-live").writes});
    }
    console.log(JSON.stringify(out));
    """
)

OVERDUE_MIN = 90
STALE_MIN = 360
GENERATED = "2026-11-02T22:00:00Z"
PUBLISHED = "2026-11-02T22:01:30Z"


def _source(
    source_id: str,
    state: str,
    *,
    read_at: str | None = GENERATED,
    observed_at: str | None = None,
    age_min: int | None = 0,
    note: str = "",
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "state": state,
        "read_at": read_at,
        "observed_at": observed_at,
        "age_min": age_min,
        "note": note,
    }


def _record(
    sources: list[dict[str, Any]], published: str | None = PUBLISHED
) -> dict[str, Any]:
    states = ("available", "partial", "overdue", "unavailable", "snapshot")
    summary: dict[str, int] = {
        s: sum(1 for x in sources if x["state"] == s) for s in states
    }
    summary["unread"] = sum(1 for x in sources if x["read_at"] is None)
    return {
        "generated_at": GENERATED,
        "lines": [],
        "data_health": {
            "generated_at": GENERATED,
            "published_at": published,
            "overdue_after_min": OVERDUE_MIN,
            "stale_after_min": STALE_MIN,
            "sources": sources,
            "summary": summary,
        },
    }


ALL_FIVE = [
    _source(
        "watcher",
        "available",
        read_at="2026-11-02T21:57:00Z",
        observed_at="2026-11-02T21:57:00Z",
        age_min=3,
    ),
    _source(
        "pool", "partial", read_at="2026-11-02T21:59:00Z", age_min=1, note="1 of 2 read"
    ),
    _source(
        "runner_fleet",
        "overdue",
        read_at="2026-11-02T20:00:00Z",
        age_min=120,
        note="read 120 min ago, limit 90",
    ),
    _source(
        "receipts",
        "unavailable",
        read_at=None,
        age_min=None,
        note="the lane receipts were not read",
    ),
    _source("linear", "snapshot", observed_at="2026-10-30", age_min=None),
    _source(
        "cloud_accounts",
        "unavailable",
        read_at=None,
        age_min=None,
        note="the cloud accounts were not read",
    ),
]


def _draw(page: str, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [node, "-e", HARNESS, str(NODE_DIR / "page" / page), json.dumps(steps)],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode()
    return cast(
        "list[dict[str, Any]]",
        json.loads(done.stdout.decode().strip().splitlines()[-1]),
    )


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_strip_block_is_the_same_on_the_dashboard_and_the_floor() -> None:
    texts = [p.read_text(encoding="utf-8") for p in (DASHBOARD, FLOOR)]
    blocks = [BLOCK.findall(t) for t in texts]
    assert [len(b) for b in blocks] == [1, 1]
    assert blocks[0] == blocks[1]
    css = [CSS_BLOCK.findall(t) for t in texts]
    assert [len(c) for c in css] == [1, 1]
    assert css[0] == css[1]
    for text in texts:
        assert 'id="hstrip"' in text
        assert 'id="hstrip-live"' in text
        assert 'aria-live="polite"' in text.split('id="hstrip-live"')[1][:80]


@pytest.mark.parametrize("page", ["index.html", "floor/index.html"])
def test_strip_separates_publish_and_read_time(page: str) -> None:
    [shown] = _draw(
        page, [{"now": "2026-11-02T22:05:00Z", "record": _record(ALL_FIVE)}]
    )
    text = _text(shown["html"])
    assert "Generated 2026-11-02 22:00 UTC" in text
    assert "Published 2026-11-02 22:01 UTC" in text
    assert "Fetched 2026-11-02 22:05 UTC" in text
    assert "Day basis UTC" in text
    # Each source's own read time is a separate, labelled value from the publish time.
    assert "watcher · read 2026-11-02 21:57 UTC" in text
    assert "pool · read 2026-11-02 21:59 UTC" in text
    # A record the publisher has not stamped says so; it is not the generated time.
    [unstamped] = _draw(
        page,
        [{"now": "2026-11-02T22:05:00Z", "record": _record(ALL_FIVE, published=None)}],
    )
    assert "Published not stamped by the publisher" in _text(unstamped["html"])


@pytest.mark.parametrize("page", ["index.html", "floor/index.html"])
def test_strip_states_readable_without_colour(page: str) -> None:
    [shown] = _draw(
        page, [{"now": "2026-11-02T22:05:00Z", "record": _record(ALL_FIVE)}]
    )
    html = shown["html"]
    text = _text(html)
    icons = {}
    for word in ("Available", "Partial", "Overdue", "Unavailable", "Snapshot"):
        found = re.search(
            rf'<span class="hs-icon" aria-hidden="true">(.)</span><b>{word}</b>', html
        )
        assert found, f"{word} has no icon beside its word"
        icons[word] = found.group(1)
    # Five states, five different shapes: no two share an icon.
    assert len(set(icons.values())) == 5
    # The strip leads with the unavailable count, then the overdue count, and never draws unread as 0.
    assert "2 sources unavailable, 1 overdue" in text
    assert text.index("2 sources unavailable") < text.index("1 overdue")
    assert "2 unread" in text
    assert "receipts · unread" in text
    assert not re.search(r"\b0 min\b", text)
    # The icon and the word carry the state, not a colour class: removing every class attribute loses nothing.
    assert "Unavailable" in re.sub(r'class="[^"]*"', "", html)
    # With no unread source the count is not drawn at all (never "0 unread").
    [clean] = _draw(
        page, [{"now": "2026-11-02T22:05:00Z", "record": _record([ALL_FIVE[0]])}]
    )
    assert "0 sources unavailable, 0 overdue" in _text(clean["html"])
    assert "unread" not in _text(clean["html"])


@pytest.mark.parametrize("page", ["index.html", "floor/index.html"])
def test_stale_page_never_looks_live(page: str) -> None:
    record = _record(ALL_FIVE[:1])
    steps = [
        {"now": "2026-11-02T22:05:00Z", "record": record},
        {"now": "2026-11-03T00:00:00Z", "record": record},  # 120 min after generation
        {"now": "2026-11-03T04:30:00Z", "record": record},  # 6.5 h after generation
        {"now": "2026-11-03T04:35:00Z", "failure": {"error": "HTTP 503"}},
    ]
    fresh, overdue, stale, failed = _draw(page, steps)

    def headline(shown: dict[str, Any]) -> str:
        found = re.search(r"<b>([^<]+)</b>", shown["html"])
        assert found
        return found.group(1)

    assert headline(fresh) == "Available"
    assert headline(overdue) == "Overdue"
    assert "Stale, not live" not in overdue["html"]
    assert headline(stale) == "Stale, not live"
    # The source rows say they are as of the generation, so a refresh of the page does not make them look live.
    assert "as of the record" in stale["html"]
    # A failed fetch keeps the last values, names the failure and the last success with its age.
    text = _text(failed["html"])
    assert "Update unavailable" in text
    assert "Last successful fetch: 2026-11-03 04:30 UTC, 5 min ago" in text
    assert "HTTP 503" in text
    assert "Generated 2026-11-02 22:00 UTC" in text
    assert "watcher · read 2026-11-02 21:57 UTC" in text
    assert re.search(r"<b>Stale, not live</b>", failed["html"])


@pytest.mark.parametrize("page", ["index.html", "floor/index.html"])
def test_stale_page_fetch_failure_with_no_prior_record_says_so(page: str) -> None:
    [failed] = _draw(
        page, [{"now": "2026-11-03T04:35:00Z", "failure": {"error": "HTTP 503"}}]
    )
    assert "Update unavailable" in failed["html"]
    assert "Unavailable" in failed["html"]
    assert "Available</b>" not in failed["html"].replace("Unavailable</b>", "")


@pytest.mark.parametrize("page", ["index.html", "floor/index.html"])
def test_strip_announces_a_state_change_once(page: str) -> None:
    record = _record(ALL_FIVE[:1])
    worse = _record(ALL_FIVE)
    steps = [
        {"now": "2026-11-02T22:05:00Z", "record": record},
        {"now": "2026-11-02T22:06:00Z", "record": record},  # only the clock moved
        {"now": "2026-11-02T22:07:00Z", "record": record},
        {"now": "2026-11-02T22:08:00Z", "record": worse},  # the state changed
        {"now": "2026-11-02T22:09:00Z", "record": worse},
        {"now": "2026-11-02T22:10:00Z", "failure": {"error": "HTTP 503"}},
        {"now": "2026-11-02T22:11:00Z", "failure": {"error": "HTTP 503"}},
    ]
    shown = _draw(page, steps)
    assert [s["liveWrites"] for s in shown] == [1, 1, 1, 2, 2, 3, 3]
    assert shown[0]["live"].startswith("Data health: Available.")
    assert "2 sources unavailable" in shown[3]["live"]
    assert "update unavailable" in shown[5]["live"]


def test_strip_is_hidden_in_the_embedded_floor_tab() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = HARNESS.replace(
        'globalThis.location = {search: ""};',
        'globalThis.location = {search: "?embed=1"};',
    )
    done = subprocess.run(
        [
            node,
            "-e",
            harness,
            str(FLOOR),
            json.dumps(
                [
                    {
                        "now": "2026-11-02T22:05:00Z",
                        "record": copy.deepcopy(_record(ALL_FIVE)),
                    }
                ]
            ),
        ],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode()
    [shown] = json.loads(done.stdout.decode().strip().splitlines()[-1])
    assert shown["html"] == ""
    assert shown["liveWrites"] == 0
