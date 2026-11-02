"""Plain-language, time-zone, sizing and summary rules for the Factory floor page (owned page: page/floor/index.html)."""

from __future__ import annotations

import re
from pathlib import Path

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect"
)
PAGE = (NODE_DIR / "page/floor/index.html").read_text(encoding="utf-8")
CSS = "\n".join(re.findall(r"<style>(.*?)</style>", PAGE, re.DOTALL))
_SCRIPT_MATCH = re.search(r"<script>(.*)</script>", PAGE, re.DOTALL | re.IGNORECASE)
assert _SCRIPT_MATCH is not None
SCRIPT = _SCRIPT_MATCH.group(1)
BODY = PAGE.split("</style>")[-1]


def test_floor_tab_has_no_second_summary() -> None:
    assert 'id="andon"' not in PAGE
    assert 'id="tally"' not in PAGE
    assert 'id="shiftProse"' not in PAGE
    assert 'id="verdict"' in PAGE
    assert "function renderVerdict" in SCRIPT


def test_floor_tab_uses_the_shared_glossary() -> None:
    visible = re.sub(r"<script>.*?</script>", "", BODY, flags=re.DOTALL | re.IGNORECASE)
    for old in ("andon cord", "Andon board", "Shift report", "ledger row"):
        assert old.lower() not in visible.lower(), old
    assert 'stopped:"Stopped"' in SCRIPT
    assert "failing checks" in SCRIPT
    assert "CI red" not in SCRIPT
    assert "Worker " in SCRIPT


def test_floor_tab_shows_eastern_time_and_keeps_utc_in_a_hover_title() -> None:
    assert 'const ET = "America/New_York"' in SCRIPT
    assert 'UTC">' in SCRIPT
    assert "UTC · " not in SCRIPT
    assert not re.search(r"toISOString\(\)[^;]*\)\} UTC</small>", SCRIPT)


def test_floor_tab_slot_strips_carry_counts_and_a_legend() -> None:
    assert "running, ${stalled} stalled, ${free} free" in SCRIPT
    assert 'class="slotlegend"' in PAGE
    for word in ("Claude worker", "Codex worker", "stalled worker", "free slot"):
        assert word in PAGE


def test_floor_tab_collapses_lines_with_no_plan_file() -> None:
    assert "planned but" in SCRIPT
    assert 'class="ghosts"' in SCRIPT
    assert 'class="line ghost"' not in SCRIPT


def test_floor_tab_text_is_not_below_twelve_pixels() -> None:
    for size in re.findall(r"font(?:-size)?:(?:[0-9]+ )?([0-9]*\.?[0-9]+)rem", CSS):
        assert float(size) >= 0.75, size
    for px in re.findall(r"font-size:\s*([0-9.]+)px", CSS):
        assert float(px) >= 12, px
    assert re.search(r"small\{font-size:\.75rem\}", CSS)


def test_floor_tab_status_text_uses_darker_light_mode_tokens() -> None:
    assert "--ok-text:#1f6b38" in CSS
    assert "--warn-text:#8a5300" in CSS
    assert "--stop-text:#a3271f" in CSS
    assert ".pill.stop{color:var(--stop-text)}" in CSS
    assert ".chip.ci-green{border-color:var(--ok);color:var(--ok-text)}" in CSS


def test_floor_tab_words_use_the_body_face() -> None:
    for sel in (".eyebrow{", ".pill{", ".chip{", ".lane .tk{", ".sitenav a{"):
        rule = re.search(re.escape(sel) + r"[^}]*}", CSS)
        assert rule is not None, sel
        assert "f-mono" not in rule.group(0), sel
