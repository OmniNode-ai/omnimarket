# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Dashboard and the Floor draw one condition, an honest progress bar, memory as used over total and a refusing machine."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    NODE_DIR,
    SHELL_FILES,
    _run_floor,
    _run_shell,
    _shell_out,
)

FIXTURE: dict[str, Any] = json.loads(
    (
        NODE_DIR.parents[3]
        / "tests/nodes/node_factory_floor_publish_effect/floor_p0_record.json"
    ).read_text(encoding="utf-8")
)
DASH_SRC = (NODE_DIR / "page/index.html").read_text(encoding="utf-8")
FLOOR_SRC = (NODE_DIR / "page/floor/index.html").read_text(encoding="utf-8")
LABEL = {
    "queued": "Queued",
    "building": "Building",
    "stopped": "Stopped",
    "complete": "Complete",
    "needs_review": "Needs review",
}
LINE_IDS = [x["line_id"] for x in FIXTURE["lines"]]


def _floor(record: dict[str, Any]) -> dict[str, str]:
    return _run_floor({"factory-floor.json": record})


def _dash(record: dict[str, Any]) -> str:
    files = {
        **SHELL_FILES,
        "factory-floor.json": record,
        "publish-status.json": {"published_at": "2099-01-01T00:01:00Z", "steps": []},
    }
    return _shell_out(_run_shell(files))["dash"]


def floor_sections(out: dict[str, str]) -> dict[str, str]:
    parts = re.split(r'(?=<section class="line" id="line-)', out["lines"])
    found: dict[str, str] = {}
    for part in parts[1:]:
        m = re.match(r'<section class="line" id="line-([^"]+)"', part)
        assert m
        found[m.group(1)] = part
    return found


def dash_cards(dash: str) -> dict[str, str]:
    cards = re.split(r'(?=<article class="card)', dash)[1:]
    return dict(zip(LINE_IDS, cards, strict=False))


def conditions(html: str) -> list[str]:
    return re.findall(r'data-condition="([a-z_]+)"', html)


def test_one_condition_both_pages() -> None:
    expected = [x["condition"] for x in FIXTURE["lines"]]
    floor = floor_sections(_floor(FIXTURE))
    dash = dash_cards(_dash(FIXTURE))
    assert list(floor) == LINE_IDS
    assert list(dash) == LINE_IDS
    for line_id, want in zip(LINE_IDS, expected, strict=True):
        assert conditions(floor[line_id]) == [want], line_id
        assert conditions(dash[line_id]) == [want], line_id
        assert LABEL[want] in floor[line_id]
        assert LABEL[want] in dash[line_id]


def test_one_condition_both_pages_a_blocked_line_with_a_live_lane_never_shows_building_beside_stopped() -> (
    None
):
    floor = floor_sections(_floor(FIXTURE))["line-c"]
    dash = dash_cards(_dash(FIXTURE))["line-c"]
    for html in (floor, dash):
        assert "Building" in html
        assert "Stopped" not in html
        assert "three red PRs" in html
        assert "Evidence: repo#1, repo#2, repo#3" in html
    stopped_floor = floor_sections(_floor(FIXTURE))["line-d"]
    assert "Why it is stopped" in stopped_floor
    assert "Building" not in stopped_floor


def test_one_condition_both_pages_reads_no_retired_field_and_a_missing_condition_is_unread() -> (
    None
):
    for src in (DASH_SRC, FLOOR_SRC):
        for retired in ("status_pill", "percent_built", "andon"):
            assert retired not in src
    record = copy.deepcopy(FIXTURE)
    for line in record["lines"]:
        del line["condition"]
    assert "Condition unread" in floor_sections(_floor(record))["line-b"]
    assert "Condition unread" in dash_cards(_dash(record))["line-b"]


def pbar(html: str) -> str:
    m = re.search(r'<div class="pbar[^"]*"[^>]*>', html)
    assert m, html[:200]
    return m.group(0)


def test_progress_bar_neutral_and_honest() -> None:
    floor = floor_sections(_floor(FIXTURE))
    dash = dash_cards(_dash(FIXTURE))
    for html in (floor, dash):
        b = html["line-b"]
        assert "6 / 48 work tasks, 12.5%" in b
        assert "20 gates: 10 passed, 2 failed, 8 waiting" in b
        assert 'aria-valuenow="6"' in pbar(b)
        assert "6 of 48 work tasks complete, 12.5%" in pbar(b)
        # unknown is not zero: an unknown track, no percentage and no aria-valuenow
        e = html["line-e"]
        assert "Progress unknown" in e
        assert "aria-valuenow" not in pbar(e)
        assert "unknown" in pbar(e)
        text = re.search(r'<span class="ptext">.*?</span>', e, re.S)
        assert text
        assert "%" not in text.group(0)
        assert "No work tasks declared" in html["line-f"]
        assert "aria-valuenow" not in pbar(html["line-f"])
        # a real zero is a known denominator with nothing complete
        g = html["line-g"]
        assert "0 / 5 work tasks, 0.0%" in g
        assert 'aria-valuenow="0"' in pbar(g)
        # every task done and a gate pending is never Complete
        h = html["line-h"]
        assert "All tasks recorded done; gate G3 pending" in h
        assert "Complete" not in h
        assert "All tasks recorded done" not in html["line-i"]
        assert "Complete" in html["line-i"]
        # the evidence row keeps merged, running on dev and verified apart; an unread source is not zero
        for word in ("Merged", "Running on dev", "Verified"):
            assert word in b
        assert re.search(r"Verified <span class=\"unread\">not read</span>", b)
        assert 'Running on dev <span class="mono">0 of 4</span>' in html["line-d"]


def test_progress_bar_neutral_and_honest_colour_does_not_follow_the_condition() -> None:
    floor = floor_sections(_floor(FIXTURE))
    for src, sections in ((FLOOR_SRC, floor), (DASH_SRC, dash_cards(_dash(FIXTURE)))):
        css = "\n".join(re.findall(r"<style>(.*?)</style>", src, re.S))
        rules = re.findall(r"([^{}]*\.pbar[^{}]*)\{([^}]*)\}", css)
        assert {sel.strip() for sel, _ in rules} == {
            ".pbar",
            ".pbar b",
            ".pbar.unknown",
        }
        fill = next(body for sel, body in rules if sel.strip() == ".pbar b")
        assert "background:var(--car)" in fill
        shapes = set()
        for html in sections.values():
            m = re.search(r'<div class="pbar"[^>]*><b style="([^"]*)">', html)
            if m:
                assert re.fullmatch(r"--w:[0-9.]+%", m.group(1)), m.group(1)
                shapes.add(re.sub(r"aria-value\w+=\"[^\"]*\"", "", pbar(html)))
        assert len(shapes) == 1


def gauge(html: str, label: str) -> str:
    m = re.search(
        r'<div class="gauge"><span>'
        + re.escape(label)
        + r"</span>.*?(?=<div class=\"gauge\">|</div>\s*</div>|$)",
        html,
        re.S,
    )
    assert m, label
    return m.group(0)


def floor_bay(out: dict[str, str], host: str) -> str:
    grid = out["bayGrid"]
    start = grid.index(f'id="bay-{host}"')
    return grid[start : grid.find("</article>", start)]


def dash_bay(dash: str, host: str) -> str:
    for part in re.split(r'(?=<article class="bay)', dash)[1:]:
        if f'aria-label="{host}"' in part.split(">", 1)[0]:
            return part.split("</article>", 1)[0]
    raise AssertionError(host)


def test_memory_gauge_matches_label() -> None:
    out = _floor(FIXTURE)
    dash = _dash(FIXTURE)
    for html in (floor_bay(out, "m-open"), dash_bay(dash, "m-open")):
        g = gauge(html, "Memory used")
        assert "36 of 48 GB used" in g  # 48 total, 12 free: used, not free
        assert "--w:75%" in g  # the fill is used/total
        assert 'aria-label="Memory used: 36 of 48 GB used"' in g
        assert "Memory free" not in html
    for html in (floor_bay(out, "m-nototal"), dash_bay(dash, "m-nototal")):
        g = gauge(html, "Memory free")
        assert "9.5 GB free (total unread)" in g
        assert "gbar" not in g  # a missing total draws no bar
        assert "Memory used" not in html
    for src in (DASH_SRC, FLOOR_SRC):
        assert "1 - num(m.mem_free_gb)" not in src
        assert '"memory free"' not in src


def test_load_per_core_is_a_number() -> None:
    record = copy.deepcopy(FIXTURE)
    record["floor"]["machines"][0]["load_per_core"] = 2.5
    out = _floor(record)
    dash = _dash(record)
    for html in (floor_bay(out, "m-open"), dash_bay(dash, "m-open")):
        g = gauge(html, "Load per core")
        assert "2.50 (1.0 = one runnable task per core)" in g
        assert "gbar" not in g  # never a bar capped at one
        assert "%" not in re.sub(r"<[^>]+>", " ", g)
    assert "0.35 (1.0 = one runnable task per core)" in floor_bay(out, "m-nototal")


def test_refusing_machine_and_overflow() -> None:
    out = _floor(FIXTURE)
    dash = _dash(FIXTURE)
    reason = "Not taking new work:</b> CPU idle under the bar"
    for html in (floor_bay(out, "m-refuse"), dash_bay(dash, "m-refuse")):
        assert reason in html
        assert html.index("Not taking new work") < min(
            html.index(k) for k in ('class="cap"', 'class="slots"') if k in html
        )
        assert "<i></i>" not in html  # its free slots are not drawn as available
    over_floor = floor_bay(out, "m-over")
    over_dash = dash_bay(dash, "m-over")
    assert '<span class="mono">7/6</span>' in over_floor
    assert '<span class="mono">7/6</span>' in over_dash
    for html in (over_floor, over_dash):
        assert "1 over capacity" in html
        assert len(re.findall(r'<i class="[^"]*\bover\b', html)) == 1
        assert "Not taking new work" not in html
    # the page never clamps a running count away
    for src in (DASH_SRC, FLOOR_SRC):
        assert not re.search(r"Math\.max\(0, (running|lanes\.length)", src)
    # observability is not health: readings available apart from accepting and refusing counts
    assert "4/5<small>machine readings available</small>" in dash
    assert "3 accepting work, 1 refusing work" in dash
    assert "machines read" not in dash


def test_car_is_decorative() -> None:
    for out in (floor_sections(_floor(FIXTURE)), dash_cards(_dash(FIXTURE))):
        icons = {
            m
            for html in out.values()
            for m in re.findall(r"<svg class=\"caricon\".*?</svg>", html, re.S)
        }
        assert len(icons) == 1  # one icon for every line: it encodes no data
        (icon,) = icons
        assert 'aria-hidden="true"' in icon
        for banned in ("<title", "title=", "data-", "style="):
            assert banned not in icon
        assert all(icon in html for html in out.values())
    for src in (DASH_SRC, FLOOR_SRC):
        for gone in (
            "partStations",
            "partStates",
            "miniCar",
            "carSVG",
            "PART_ORDER",
            "const CAR ",
        ):
            assert gone not in src
        for stripe in re.findall(r'<div class="hazard"[^>]*>', src):
            assert 'aria-hidden="true"' in stripe
            assert "title=" not in stripe
