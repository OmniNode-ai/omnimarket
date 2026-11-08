# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The site serves a favicon set: an SVG, a 32 px PNG and an apple-touch icon, linked from every page's head.

Before this the pages carried an empty icon (``href="data:,"``) and no icon file was published, so a tab showed the
browser's blank page glyph. The icons are one mark (a gear) in the site's own blue; the simple floor page tints its
SVG icon by the record's level, so a tab read from across the room says green, amber or red.
"""

from __future__ import annotations

import json
import posixpath
import re
import shutil
import struct
import subprocess
import textwrap
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    load_runtime,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).resolve().parents[1]
ICONS = NODE_DIR / "page/icons"
SIMPLE = NODE_DIR / "page/simple.html"

# The palette the pages draw with (the :root colours of page/simple.html); an icon uses no other colour.
SITE_COLOURS = {"#2b6cb0", "#ffffff"}
LEVEL_COLOURS = {
    "green": "#1f7a4d",
    "amber": "#a56a00",
    "red": "#b3261e",
    "grey": "#9aa1a9",
}
PNG_SIZES = {"favicon-32.png": 32, "apple-touch-icon.png": 180}


def _pages() -> dict[str, Path]:
    """Every page the publish serves, by the name it is served under, read from the contract."""
    rt = load_runtime()
    pages = {
        rt.names.simple_page: NODE_DIR / rt.simple_page_relpath,
        rt.names.page: NODE_DIR / rt.page_relpath,
        rt.names.floor_page: NODE_DIR / rt.floor_page_relpath,
        rt.reports.index.remote: NODE_DIR / rt.reports.index.page_relpath,
    }
    for entry in (*rt.reports.entries, *rt.operator_reports.entries):
        for page in entry.pages:
            pages[page.remote] = NODE_DIR / page.page_relpath
    return pages


def _links(html: str) -> list[dict[str, str]]:
    head = html.split("</head>", 1)[0]
    return [
        dict(re.findall(r'([a-z-]+)="([^"]*)"', tag))
        for tag in re.findall(r"<link\b[^>]*>", head)
    ]


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_golden_the_contract_names_the_three_icons_and_each_file_exists() -> None:
    rt = load_runtime()
    assert rt.names.icons == ("favicon.svg", "favicon-32.png", "apple-touch-icon.png")
    for name in rt.names.icons:
        assert (NODE_DIR / rt.icons_relpath / name).stat().st_size > 0


def test_golden_the_png_icons_are_the_sizes_their_names_and_link_tags_say() -> None:
    for name, size in PNG_SIZES.items():
        assert _png_size((ICONS / name).read_bytes()) == (size, size)


def test_golden_the_svg_is_one_mark_in_the_sites_own_colours_with_no_outside_artwork() -> (
    None
):
    text = (ICONS / "favicon.svg").read_text(encoding="utf-8")
    root = ET.fromstring(text)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.attrib["viewBox"] == "0 0 64 64"
    colours = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}", text)}
    assert colours == SITE_COLOURS
    assert not re.search(
        r"<image|<script|<style|href=|url\(|https?://(?!www\.w3\.org/2000/svg)", text
    )


def test_golden_every_page_head_links_the_svg_the_32px_png_and_the_apple_touch_icon_by_a_path_that_resolves() -> (
    None
):
    rt = load_runtime()
    published = set(rt.names.icons)
    assert len(_pages()) == 7
    for served_as, source in _pages().items():
        links = _links(source.read_text(encoding="utf-8"))
        icons = [
            link
            for link in links
            if link["rel"] in ("icon", "apple-touch-icon") or "icon" in link["rel"]
        ]
        assert [(i["rel"], i.get("type"), i.get("sizes")) for i in icons] == [
            ("icon", "image/svg+xml", None),
            ("icon", "image/png", "32x32"),
            ("apple-touch-icon", None, None),
        ], served_as
        resolved = [
            posixpath.normpath(
                posixpath.join(posixpath.dirname(served_as), unquote(i["href"]))
            )
            for i in icons
        ]
        assert resolved == ["favicon.svg", "favicon-32.png", "apple-touch-icon.png"], (
            served_as
        )
        assert set(resolved) == published


def test_error_no_page_still_carries_the_empty_icon() -> None:
    for served_as, source in _pages().items():
        assert 'href="data:,"' not in source.read_text(encoding="utf-8"), served_as


HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, recordJson, svgPath] = process.argv.slice(1);
    const script = /<script>([\\s\\S]*?)<\\/script>/.exec(fs.readFileSync(pagePath, "utf8"))[1];
    const els = {};
    const mk = id => ({id, innerHTML: "", textContent: "", href: "", className: "", hidden: false, attrs: {}, style: {}, dataset: {},
      setAttribute(k, v) { this.attrs[k] = v; }, addEventListener() {}});
    globalThis.document = {title: "", getElementById: id => els[id] || (els[id] = mk(id)), addEventListener() {},
      createElement: () => mk("created"), body: {append() {}}, querySelectorAll: () => [], activeElement: null};
    // The shared panel (floor-panel.js) has its own tests; the page's script only configures and refreshes it.
    globalThis.FloorPanel = {configure() {}, sync() {}, update() {}, isOpen: () => false, restoring: false};
    globalThis.window = {addEventListener() {}};
    globalThis.location = {hash: ""};
    globalThis.setInterval = () => 0;
    globalThis.fetch = async url => {
      if (url.startsWith("favicon.svg")) {
        return {ok: true, status: 200, text: async () => fs.readFileSync(svgPath, "utf8")};
      }
      return {ok: true, status: 200, json: async () => JSON.parse(recordJson)};
    };
    (async () => {
      new Function(script).call(globalThis);
      await new Promise(r => setTimeout(r, 150));
      console.log(JSON.stringify({href: (els["icon-svg"] || {}).href || ""}));
    })();
    """
)


def _record(level: str, age_s: int = 0) -> dict[str, object]:
    return {
        "level": level,
        "sentence": "s",
        "subline": "",
        "generated_at": datetime.fromtimestamp(
            datetime.now(UTC).timestamp() - age_s, UTC
        ).isoformat(),
        "numbers": [],
        "alarms": [],
        "causes": [],
        "causes_shown": 5,
        "rebuild": {"current_piece": 1, "headline": "", "next_text": ""},
        "pieces": [],
        "lab_state": [],
        "red_rule": {
            "state": "WORKING",
            "red_rows": 0,
            "red_with_work_under_way": 0,
            "violations": [],
        },
        "chain_findings": [],
        "outside_goal": None,
        "details_href": "detail.html",
    }


def _icon_href_after_draw(record: dict[str, object]) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [
            node,
            "-e",
            HARNESS,
            str(SIMPLE),
            json.dumps(record),
            str(ICONS / "favicon.svg"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    href = str(json.loads(done.stdout)["href"])
    return href


@pytest.mark.parametrize("level", ["green", "amber", "red"])
def test_golden_the_simple_page_tints_its_svg_icon_by_the_records_level(
    level: str,
) -> None:
    href = _icon_href_after_draw(_record(level))
    prefix = "data:image/svg+xml,"
    assert href.startswith(prefix)
    svg = unquote(href[len(prefix) :])
    base = (ICONS / "favicon.svg").read_text(encoding="utf-8")
    assert svg == base.replace("#2b6cb0", LEVEL_COLOURS[level])
    assert "#2b6cb0" not in svg


def test_error_a_record_past_its_age_bound_or_with_an_unknown_level_draws_grey_never_a_level_colour() -> (
    None
):
    prefix = "data:image/svg+xml,"
    for record in (_record("green", age_s=7200), _record("purple")):
        svg = unquote(_icon_href_after_draw(record)[len(prefix) :])
        assert LEVEL_COLOURS["grey"] in svg
        assert LEVEL_COLOURS["green"] not in svg
