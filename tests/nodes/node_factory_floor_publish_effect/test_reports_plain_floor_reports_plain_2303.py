"""The three report pages read plainly: findings F11, F12, F13, F16 and the report-page part of F03, F05, F14, F15."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect"
)
PAGES = {
    name: NODE_DIR / f"page/reports/{name}/index.html"
    for name in ("process-health", "decision-map", "decision-quality")
}


def _text(name: str) -> str:
    return PAGES[name].read_text(encoding="utf-8")


def _markup(name: str) -> str:
    page = _text(name)
    return page[page.index("<body>") : page.index("<script")]


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_plain_reports_use_the_board_type_and_tokens(name: str) -> None:
    page = _text(name)
    assert '"Big Shoulders Display"' in page
    assert '"Public Sans"' in page
    assert "Georgia" not in page
    assert "#ece" in page.lower() or "#eceee9" in page.lower()


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_plain_reports_have_no_text_below_twelve_pixels(name: str) -> None:
    css = re.search(r"<style>\n/\*.*?</style>", _text(name), re.DOTALL)
    assert css
    for size in re.findall(r"font(?:-size)?:[^;}]*?(\d*\.?\d+)(rem|px)", css.group(0)):
        value = float(size[0]) * (16 if size[1] == "rem" else 1)
        assert value >= 12 or value in (1, 0), size


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_plain_reports_show_eastern_time_with_utc_only_in_a_title(
    name: str,
) -> None:
    page = _text(name)
    assert "America/New_York" in page
    assert 'title="${' in page
    assert " UTC" not in _markup(name)


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_plain_reports_markup_uses_the_glossary(name: str) -> None:
    markup = _markup(name)
    for jargon in ("lane", "andon", "ledger", "CI red", " PR "):
        assert jargon not in markup.replace("Lane ", ""), jargon
    assert "const GLOSSARY" in _text(name)


def test_page_plain_process_status_words_use_the_darker_light_tokens() -> None:
    page = _text("process-health")
    for token in ("--ok-ink:#1f6b38", "--warn-ink:#8a5300", "--stop-ink:#a3271f"):
        assert token in page
    assert ".verdict.better{color:var(--ok-ink)}" in page
    assert ".verdict.worse{color:var(--stop-ink)}" in page


def test_page_plain_process_opens_on_what_changed_and_folds_the_sources() -> None:
    page = _text("process-health")
    assert page.index("What changed most") < page.index("Quality, before and after")
    assert page.index("Quality by day") < page.index('class="sources"')
    assert '<details class="sources"><summary>Sources and what was read' in page
    assert "lower is better" in page
    assert "slice(0, 5)" in page


def test_page_plain_decision_map_opens_on_the_table_and_a_bar_with_the_network_optional() -> (
    None
):
    markup = _markup("decision-map")
    assert (
        markup.index("Largest problems")
        < markup.index("How many problems stop at each stage")
        < markup.index('id="net"')
    )
    assert 'id="net" hidden' in markup
    assert 'id="netbtn"' in markup
    assert "Show the network view" in markup
    page = _text("decision-map")
    assert "slice(0,10)" in page
    assert (
        "draw();\n}"
        not in page.split("function init()")[1].split("function debounce")[0]
    )


def test_page_plain_decision_map_stage_colours_never_reuse_red_amber_or_green() -> None:
    page = _text("decision-map")
    for token in re.findall(r"--o-[a-z]+:(#[0-9a-f]{6})", page):
        r, g, b = (int(token[i : i + 2], 16) for i in (1, 3, 5))
        reddish = r > 200 and g < 120 and b < 120
        greenish = g > r + 30 and g > b + 30
        amberish = r > 200 and g > 140 and b < 80
        assert not reddish, token
        assert not greenish, token
        assert not amberish, token


def test_page_plain_decision_quality_opens_on_the_answer_and_folds_the_provenance() -> (
    None
):
    markup = _markup("decision-quality")
    assert "Jump to section" in markup
    assert 'id="how"' in markup
    page = _text("decision-quality")
    assert "How this was made" in page
    assert "firstH2" in page
