# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared helpers for the four-tab redesign tests (mockups pages 4 to 7).

A tab is opened on a stub runner with a fixed model selection, then the whole
drawn screen is read back as text, as a person reads it.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from textual.pilot import Pilot

from omnimarket.models.model_catalog_entry import ModelCatalogEntry
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiSelection,
    ModelTuiStatus,
    OnexTui,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import _root, _Runner

SIZES = [(80, 24), (140, 40)]


def entry(
    model: str,
    *,
    context: int | None,
    price_in: str,
    price_out: str,
    status: str = "ready",
    provider: str = "openrouter",
    caps: tuple[str, ...] = ("Tools",),
) -> ModelCatalogEntry:
    return ModelCatalogEntry(
        provider=provider,
        model_id=model,
        display_name=model,
        deployment="cloud",
        context_tokens=context,
        input_usd_per_million=Decimal(price_in),
        output_usd_per_million=Decimal(price_out),
        capabilities=dict.fromkeys(caps, "yes"),
        status=status,
    )


def selection() -> ModelTuiSelection:
    """The mockup's three models, qwen3-27b tried first."""
    return ModelTuiSelection(
        entries=(
            entry("qwen3-27b", context=32_000, price_in="0.10", price_out="0.30"),
            entry(
                "gemini-flash",
                context=1_000_000,
                price_in="0.08",
                price_out="0.30",
                caps=("Tools", "Vision"),
            ),
            entry(
                "glm-5",
                context=128_000,
                price_in="0.20",
                price_out="0.60",
                status="unavailable",
            ),
        ),
        first=("openrouter", "qwen3-27b"),
    )


def app(
    run: _Runner | None = None,
    hooks: dict[str, str] | None = None,
    delegation: str = "hook",
    **extra: Any,
) -> OnexTui:
    async def status() -> ModelTuiStatus:
        return ModelTuiStatus(
            models=3,
            delegation=delegation,
            hooks=hooks or {"prompt_hook": "installed"},
        )

    return OnexTui(root=_root, runner=run or _Runner([]), load_status=status, **extra)


async def settle(pilot: Pilot[int], times: int = 10) -> None:
    for _ in range(times):
        await pilot.pause()


async def open_tab(pilot: Pilot[int], the_app: OnexTui, name: str) -> None:
    """Open one tab straight on the fixed selection."""
    await settle(pilot, 4)
    await the_app.push_screen(the_app._build_tab(name, selection()))
    await settle(pilot)


async def open_add_models(pilot: Pilot[int], the_app: OnexTui) -> None:
    """The Model tab's add-models screen on the fixed selection (OMN-26688)."""
    await settle(pilot, 4)
    the_app._selection = selection()
    await the_app.open_add_models()
    await settle(pilot)


def screen_text(the_app: OnexTui) -> str:
    """Every line the screen draws, as text (what the user reads)."""
    strips = the_app.screen._compositor.render_strips()
    return "\n".join(strip.text.rstrip() for strip in strips)


def bold_words(the_app: OnexTui) -> set[str]:
    """Every run of text the screen draws in bold, split on spaces."""
    found: set[str] = set()
    for strip in the_app.screen._compositor.render_strips():
        for segment in strip:
            if segment.style is not None and segment.style.bold:
                found.update(segment.text.split())
    return found


#: What a failure looks like when the cause leaks: an errno, an exception name,
#: a permission message, an environment name and a path (the path is the test's
#: own temporary folder, so no absolute path is written in a test).
RAW_PARTS = (
    "Errno",
    "keys.json",
    "OSError",
    "Permission denied",
    "Traceback",
    "ONEX_SECRET_DIR",
)


def planted_path(tmp_path: Path) -> Path:
    return tmp_path / "onex" / "keys.json"


def raw_text(tmp_path: Path) -> str:
    return (
        f"[Errno 13] Permission denied: '{planted_path(tmp_path)}' "
        "(OSError, set ONEX_SECRET_DIR)"
    )


def assert_no_raw(text: str, tmp_path: Path) -> None:
    """AC9: none of the planted raw cause, nor its path, is on the screen."""
    for part in (*RAW_PARTS, str(tmp_path), "pytest-of-"):
        assert part not in text, (part, text)
