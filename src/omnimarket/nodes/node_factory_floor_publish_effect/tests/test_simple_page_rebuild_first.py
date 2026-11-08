# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The main page opens with the rebuild, and every piece tile keeps its own state colour (OMN-23135).

The operator, 2026-11-08 about 12:16Z, reading the floor on a phone: the rebuild belongs at the top of the page, and pieces
2 and 3, both drawn amber in the served record, looked different. The page's ``.p.cur`` rule painted the current piece
solid blue and so hid its state colour, and the page had no legend. These tests run the page's own script under node
against a served record whose current piece is 2 and which draws pieces 2 and 3 amber, then read the drawn tiles and
resolve their background and border from the page's own style sheet. The failing control is the page as it stood before
the change (the rebuild after the holdups, ``cur`` replacing the colour, no legend): it fails every check here.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from omnimarket.nodes.node_factory_floor_publish_effect.tests.test_simple_page_machines import (
    SIMPLE,
    SIMPLE_SRC,
    _run,
)

NOW = "2026-11-06T18:40:00Z"
AMBER = "#a56a00"
GREEN = "#1f7a4d"
RED = "#b3261e"
BLUE = "#2b6cb0"
GREY = "#e6e8eb"


def _record(current: int = 2) -> dict[str, Any]:
    record: dict[str, Any] = copy.deepcopy(SIMPLE)
    record["generated_at"] = "2026-11-06T18:39:00Z"
    record["rebuild"]["current_piece"] = current
    for piece in record["pieces"]:
        number = piece["piece"]
        if number == 1:
            piece["status"], piece["draw"] = "working", "green"
        elif number in (2, 3):
            piece["status"], piece["draw"] = "working", "amber"
        elif number == 4:
            piece["status"], piece["draw"] = "not_working", "red"
        else:
            piece["status"], piece["draw"] = "not_started", "grey"
    return record


def _page(src: str = SIMPLE_SRC, current: int = 2) -> str:
    app = _run({"floor-simple.json": _record(current)}, src=src, now=NOW)["app"]
    assert isinstance(app, str)
    return app


def _tile_classes(html: str, piece: int) -> list[str]:
    match = re.search(
        rf'<a class="([^"]*)"[^>]*? href="#piece-{piece}" data-piece="{piece}"', html
    )
    assert match is not None, f"the page drew no tile for piece {piece}"
    return match.group(1).split()


def _root_vars(src: str) -> dict[str, str]:
    root = re.search(r":root\{([^}]*)\}", src)
    assert root is not None
    return dict(re.findall(r"(--[\w-]+):([^;]+)", root.group(1)))


def _style(src: str, classes: list[str]) -> dict[str, str]:
    """The declarations the page's own style sheet applies to a ``.p`` tile with these classes (the narrow-screen block
    is left out: it only resizes). More classes in a selector win, a later rule wins a tie."""
    sheet = re.search(r"<style>(.*?)</style>", src, re.S)
    assert sheet is not None
    css = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", sheet.group(1))
    variables = _root_vars(src)
    matched: list[tuple[int, int, str]] = []
    for order, (selectors, body) in enumerate(re.findall(r"([^{}]+)\{([^{}]*)\}", css)):
        for selector in selectors.split(","):
            parts = selector.strip().split(".")
            if parts[0] != "" or parts[1:2] != ["p"] or ":" in selector:
                continue
            if all(part in classes for part in parts[1:]):
                matched.append((len(parts) - 1, order, body))
    style: dict[str, str] = {}
    for _specificity, _order, body in sorted(matched):
        for declaration in body.split(";"):
            if ":" not in declaration:
                continue
            name, value = declaration.split(":", 1)
            style[name.strip()] = re.sub(
                r"var\((--[\w-]+)\)", lambda m: variables[m.group(1)], value.strip()
            )
    return style


def _marked(style: dict[str, str]) -> bool:
    border = style.get("border", "")
    outline = style.get("outline", "")
    return "solid" in border or "solid" in outline


def test_golden_the_rebuild_is_the_first_section_of_the_page_above_the_holdups() -> (
    None
):
    html = _page()
    headings = re.findall(r"<h2>(.*?)</h2>", html)
    assert headings[0] == "The rebuild"
    assert headings.index("The rebuild") < headings.index(
        "What's holding things up, biggest first"
    )
    sections = re.findall(r'<section class="(\w+)', html)
    assert sections[0] == "rebuild"
    rebuild = html[html.index('<section class="rebuild"') :]
    rebuild = rebuild[: rebuild.index("</section>")]
    assert 'class="strip"' in rebuild
    assert "Piece 1, code graph: started." in rebuild
    assert "Next is hosts and capacity." in rebuild


def test_golden_every_tile_keeps_its_state_colour_and_only_the_current_one_is_outlined() -> (
    None
):
    html = _page(current=2)
    expect = {1: GREEN, 2: AMBER, 3: AMBER, 4: RED, 5: GREY}
    for piece, colour in expect.items():
        style = _style(SIMPLE_SRC, _tile_classes(html, piece))
        assert style["background"] == colour, f"piece {piece} lost its state colour"
        assert style["background"] != BLUE
        assert _marked(style) == (piece == 2), f"piece {piece} outline wrong: {style}"


def test_golden_pieces_2_and_3_draw_the_same_background_and_only_2_is_outlined() -> (
    None
):
    html = _page(current=2)
    two = _style(SIMPLE_SRC, _tile_classes(html, 2))
    three = _style(SIMPLE_SRC, _tile_classes(html, 3))
    assert two["background"] == three["background"] == AMBER
    assert _marked(two)
    assert not _marked(three)
    assert "cur" in _tile_classes(html, 2)
    assert "amber" in _tile_classes(html, 2)


def test_golden_the_outline_follows_the_current_piece() -> None:
    html = _page(current=3)
    assert _marked(_style(SIMPLE_SRC, _tile_classes(html, 3)))
    assert not _marked(_style(SIMPLE_SRC, _tile_classes(html, 2)))
    assert len(re.findall(r'<a class="p [^"]*\bcur\b', html)) == 1


def test_golden_a_one_line_legend_under_the_strip_names_each_colour_and_the_outline() -> (
    None
):
    html = _page()
    strip_end = html.index("</div>", html.index('class="strip"'))
    legend = re.search(r'<div class="legend"[^>]*>(.*?)</div>', html[strip_end:], re.S)
    assert legend is not None, "no legend under the strip"
    assert html[strip_end:].index('class="legend"') < html[strip_end:].index(
        'class="piece"'
    )
    text = re.sub(r"<[^>]+>", " ", legend.group(1)).lower()
    for word in ("green", "amber", "red", "grey", "outline"):
        assert word in text, f"the legend does not name {word}"


def test_golden_tap_to_inspect_and_focus_keeping_hooks_are_kept() -> None:
    html = _page()
    for piece in range(1, 19):
        assert f'href="#piece-{piece}" data-piece="{piece}" data-fk="s{piece}"' in html
    assert 'aria-current="true"' in html
    assert "document.activeElement.getAttribute" in SIMPLE_SRC
    assert 'app.querySelector(`[data-fk="${held}"]`)' in SIMPLE_SRC


def test_failing_control_the_page_as_it_stood_reads_blue_and_has_no_legend() -> None:
    """The same page with the three changes taken out of it, one named break each, fails the reads above."""
    cur_rule = re.search(r"\.p\.cur\{[^}]*\}", SIMPLE_SRC)
    assert cur_rule is not None
    painted_blue = SIMPLE_SRC.replace(
        cur_rule.group(0), ".p.cur{background:var(--blue);color:#fff;font-weight:650}"
    )
    html = _page(src=painted_blue)
    two = _style(painted_blue, _tile_classes(html, 2))
    assert two["background"] == BLUE != AMBER  # the break hides tile 2's state colour

    legend_line = re.search(r'  h \+= `<div class="legend".*\n', SIMPLE_SRC)
    assert legend_line is not None
    no_legend = SIMPLE_SRC.replace(legend_line.group(0), "")
    assert 'class="legend"' not in _page(src=no_legend)
    assert 'class="legend"' in _page()

    holdups_first = SIMPLE_SRC.replace(
        '<section class="rebuild"><h2>The rebuild</h2>',
        '<h2>What\'s holding things up, biggest first</h2><section class="rebuild"><h2>The rebuild</h2>',
    )
    assert holdups_first != SIMPLE_SRC
    assert re.findall(r"<h2>(.*?)</h2>", _page(src=holdups_first))[0] != "The rebuild"
    assert re.findall(r"<h2>(.*?)</h2>", _page())[0] == "The rebuild"


def test_golden_the_page_script_loads_in_a_browser_without_a_redeclared_name() -> None:
    """The page's script once declared ``const num`` twice at the top level, a SyntaxError that left the page blank
    (the second came with OMN-23081). Every top-level const, let and function name is declared once."""
    script = re.search(r"<script>(.*?)</script>", SIMPLE_SRC, re.S)
    assert script is not None
    names = re.findall(
        r"^(?:const|let|function)\s+([A-Za-z_$][\w$]*)", script.group(1), re.M
    )
    assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)
