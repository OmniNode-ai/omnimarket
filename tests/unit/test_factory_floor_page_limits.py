# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Limits panel of the Factory floor page (OMN-22010; owned page: page/floor/index.html)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.models.model_factory_floor_input import ModelFloorCloudAccount
from omnimarket.models.model_floor_limits_input import (
    ModelFloorHostTokenPool,
    ModelFloorIdleHostReading,
    ModelFloorIdleReading,
    ModelFloorLimitsConfig,
    ModelFloorLimitsInput,
    ModelFloorPoolToken,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_limits import (
    build_limits,
)

pytestmark = pytest.mark.unit

PAGE = (
    Path(__file__).resolve().parents[2]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/floor/index.html"
).read_text(encoding="utf-8")
_SCRIPT = re.search(r"<script>(.*)</script>", PAGE, re.DOTALL | re.IGNORECASE)
assert _SCRIPT is not None
SCRIPT = _SCRIPT.group(1)
CSS = "\n".join(re.findall(r"<style>(.*?)</style>", PAGE, re.DOTALL))
NOW = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)


def _function(name: str) -> str:
    start = SCRIPT.index(f"function {name}(")
    depth, i = 0, SCRIPT.index("{", start)
    first = i
    while True:
        depth += {"{": 1, "}": -1}.get(SCRIPT[i], 0)
        if depth == 0 and i > first:
            return SCRIPT[start : i + 1]
        i += 1


def _record() -> dict[str, object]:
    """A real Limits section from the builder: one fresh pool with a revoked token, one account never read."""
    result = build_limits(
        ModelFloorLimitsInput(
            now=NOW,
            config=ModelFloorLimitsConfig(
                publish_cycle_seconds=300,
                api_budget_usd=200,
                projection_min_elapsed_hours=24,
            ),
            cloud_accounts=(
                ModelFloorCloudAccount(
                    name="h200",
                    rate_type="seven_day",
                    rate_utilization=0.62,
                    rate_resets_at="2026-11-04T22:00:00Z",
                    rate_read_at="2026-11-03T15:59:00Z",
                    credit_remaining=244.0,
                    credit_read_at="2026-11-03T15:59:00Z",
                    credit_expires="2026-11-05",
                ),
                ModelFloorCloudAccount(name="h201"),
            ),
            cloud_accounts_source="cloud-accounts-v2.json",
            token_pool=(
                ModelFloorHostTokenPool(
                    host="h200",
                    read_at="2026-11-03T15:59:00Z",
                    tokens=(
                        ModelFloorPoolToken(account="a", state="usable"),
                        ModelFloorPoolToken(account="b", state="revoked"),
                    ),
                ),
            ),
            idle=ModelFloorIdleReading(
                day="2026-11-03",
                hosts=(
                    ModelFloorIdleHostReading(
                        host="h200", state="read", slots=8, idle=3
                    ),
                    ModelFloorIdleHostReading(host="h201", state="unread"),
                ),
                idle_total=3,
                idle_unread=1,
            ),
        )
    )
    return {"limits": result.model_dump(mode="json")}


def _render(floor: dict[str, object]) -> str:
    node = shutil.which("node")
    assert node is not None, "the page render test requires Node.js"
    program = (
        "const card = {hidden: true, innerHTML: ''}; "
        "const el = () => card, esc = String;\n"
        + SCRIPT[
            SCRIPT.index("const LIMIT_GROUPS") : SCRIPT.index(
                "function limitValueText("
            )
        ]
        + "\n".join(
            _function(n)
            for n in (
                "limitValueText",
                "limitReadText",
                "limitRowHTML",
                "limitIdleHTML",
                "renderLimits",
            )
        )
        + f"\nrenderLimits({json.dumps(floor)});"
        + "\nprocess.stdout.write(JSON.stringify({hidden: card.hidden, html: card.innerHTML}));"
    )
    done = subprocess.run(
        [node, "-e", program], capture_output=True, text=True, timeout=30, check=True
    )
    return done.stdout


def test_limits_panel_is_in_the_page_hidden_until_the_record_carries_it() -> None:
    assert re.search(r'<section[^>]*id="limits"[^>]*hidden', PAGE)
    assert "renderLimits(floor)" in _function("render")
    assert json.loads(_render({}))["hidden"] is True


def test_limits_panel_draws_every_row_with_its_source_and_read_time() -> None:
    out = json.loads(_render(_record()))
    html = out["html"]
    assert out["hidden"] is False
    for title in (
        "Claude subscription accounts",
        "Claude cloud-session credit",
        "Anthropic API keys",
        "Codex (ChatGPT)",
        "GLM coding plan",
        "Lab model rungs",
        "Claude login pool, per lab host",
    ):
        assert title in html, title
    assert "Source: cloud-accounts-v2.json#h200.rate" in html
    assert "read 1 min before this record" in html
    assert "seven day utilization" in html
    assert "62%" in html
    assert "$244.00" in html


def test_limits_panel_draws_unread_as_unknown_and_never_as_zero() -> None:
    html = json.loads(_render(_record()))["html"]
    # h201 has no rate headers: its row is UNKNOWN with the cause, and carries no number.
    h201 = html.split('data-row="claude_subscription:h201"')[1].split("</li>")[0]
    assert "UNKNOWN</b>: no rate headers recorded for the account" in h201
    assert "<dl>" not in h201
    # A value the record carries as null inside a read row also reads UNKNOWN.
    h200 = html.split('data-row="claude_subscription:h200"')[1].split("</li>")[0]
    assert 'five hour utilization</dt><dd class="mono unk">UNKNOWN' in h200
    # The sources nobody wrote draw UNKNOWN rows, not zero ones.
    assert html.count('class="lrow unread"') >= 5
    assert 'dd class="mono">0%' not in html


def test_limits_panel_counts_a_revoked_token_as_revoked_beside_the_usable_ones() -> (
    None
):
    html = json.loads(_render(_record()))["html"]
    pool = html.split('data-row="account_pool:h200"')[1].split("</li>")[0]
    assert '<dt>usable</dt><dd class="mono">1</dd>' in pool
    assert '<dt>revoked</dt><dd class="mono">1</dd>' in pool


def test_limits_panel_shows_per_line_idle_and_the_days_idle_slot_hours_beside_the_rows() -> (
    None
):
    html = json.loads(_render(_record()))["html"]
    assert "Idle, 2026-11-03 (Eastern day)" in html
    assert "Idle slot-hours today" in html
    # The first record of the day has no interval yet: the hours read UNKNOWN, not 0.0.
    assert 'UNKNOWN</span><div class="muted">Idle slot-hours today' in html
    assert "<b>h200</b> 3 idle of 8" in html
    assert "<b>h201</b> UNKNOWN (not read)" in html
    # The idle block sits in the same grid as the rows.
    assert html.index("Claude login pool") < html.index("Idle, 2026-11-03")
    assert html.count('class="lgrid"') == 1


def test_limits_panel_text_is_not_below_twelve_pixels() -> None:
    for size in re.findall(r"font(?:-size)?:(?:[0-9]+ )?([0-9]*\.?[0-9]+)rem", CSS):
        assert float(size) >= 0.75, size
