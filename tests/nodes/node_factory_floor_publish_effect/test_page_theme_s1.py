# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The board's state colours come from the vendored OmniUI theme sets, and the vendored files are pinned by sha256."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers import (
    handler_factory_floor_publish as handler_module,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    NODE_DIR,
    HandlerFactoryFloorPublish,
    load_runtime,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    LocalSsh,
    _request,
)

pytestmark = pytest.mark.unit

VENDOR_DIR = NODE_DIR / "page/vendor/omniui"
PIN_FILE = VENDOR_DIR / "PIN.sha256"
PAGE = (NODE_DIR / "page/index.html").read_text(encoding="utf-8")

# The page variable -> the OmniUI variable it aliases (S1 of the view-centric plan).
ALIASES = {
    "--floor": "--onex-color-background-primary",
    "--panel": "--onex-color-background-secondary",
    "--ink": "--onex-color-text-primary",
    "--muted": "--onex-color-text-secondary",
    "--rule": "--onex-color-border-default",
    "--ok": "--onex-color-status-success",
    "--warn": "--onex-color-status-warning",
    "--stop": "--onex-color-status-error",
}
THEMES = ("light", "dark")


def _pins(pin_text: str) -> dict[str, str]:
    """``name -> sha256`` from a sha256sum-format pin file; ``#`` lines are comments."""
    pins: dict[str, str] = {}
    for line in pin_text.splitlines():
        if line.strip() and not line.startswith("#"):
            digest, name = line.split(None, 1)
            pins[name.strip()] = digest
    return pins


def _theme_values(css: str, theme: str) -> dict[str, str]:
    """The custom properties the compiled CSS sets under ``[data-onex-theme='onex.theme.<theme>']``."""
    block = re.search(
        rf"\[data-onex-theme='onex\.theme\.{theme}'\]\s*\{{(.*?)\}}", css, re.DOTALL
    )
    assert block is not None, theme
    return dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", block.group(1)))


def _root_aliases(page: str) -> dict[str, str]:
    """``--name -> var name`` for each ``--name:var(--x)`` the page's first (:root) rule declares."""
    root = re.search(r":root\{(.*?)\n\}", page, re.DOTALL)
    assert root is not None
    return dict(re.findall(r"(--[a-z0-9-]+):\s*var\((--[a-z0-9-]+)\)", root.group(1)))


def _resolve(page: str, css_by_theme: dict[str, str], theme: str, name: str) -> str:
    """What the browser computes for ``name`` on the root carrying the theme: the alias, then the theme's value."""
    target = _root_aliases(page)[name]
    return _theme_values(css_by_theme[theme], theme)[target]


def _vendored_css() -> dict[str, str]:
    return {
        t: (VENDOR_DIR / f"onex.theme.{t}.css").read_text(encoding="utf-8")
        for t in THEMES
    }


def test_pin_every_vendored_file_matches_its_sha256() -> None:
    pins = _pins(PIN_FILE.read_text(encoding="utf-8"))
    names = set(load_runtime().names.theme_css)
    assert set(pins) == names
    for name, digest in pins.items():
        assert hashlib.sha256((VENDOR_DIR / name).read_bytes()).hexdigest() == digest, (
            f"{name} differs from its pin: recapture it from OmniUI and update PIN.sha256"
        )


def test_pin_fails_when_a_vendored_file_differs_from_the_pin(tmp_path: Path) -> None:
    """Positive control: the comparison the pin test makes does fail on a one-byte change."""
    pins = _pins(PIN_FILE.read_text(encoding="utf-8"))
    name = "onex.theme.light.css"
    changed = tmp_path / name
    changed.write_bytes((VENDOR_DIR / name).read_bytes() + b" ")
    assert hashlib.sha256(changed.read_bytes()).hexdigest() != pins[name]


def test_state_colours_resolve_from_the_vendored_theme_in_both_themes() -> None:
    css = _vendored_css()
    for theme in THEMES:
        values = _theme_values(css[theme], theme)
        for name, target in ALIASES.items():
            assert _root_aliases(PAGE)[name] == target
            assert _resolve(PAGE, css, theme, name) == values[target]
    # The two themes differ for the state colours, so the page cannot be reading one fixed value.
    assert _resolve(PAGE, css, "light", "--stop") != _resolve(
        PAGE, css, "dark", "--stop"
    )


def test_changing_one_status_token_in_a_copy_changes_the_stop_colour() -> None:
    css = _vendored_css()
    before = _resolve(PAGE, css, "dark", "--stop")
    changed = dict(css)
    changed["dark"] = css["dark"].replace(
        "--onex-color-status-error: #ef4444;", "--onex-color-status-error: #123456;"
    )
    assert changed["dark"] != css["dark"]
    assert _resolve(PAGE, changed, "dark", "--stop") == "#123456"
    assert before != "#123456"


def test_removing_an_alias_makes_the_assertion_fail() -> None:
    css = _vendored_css()
    unaliased = PAGE.replace(
        "--stop:var(--onex-color-status-error);", "--stop:#c3352b;"
    )
    assert unaliased != PAGE
    with pytest.raises(KeyError):
        _resolve(unaliased, css, "light", "--stop")


def test_every_theme_set_defines_every_aliased_name() -> None:
    """A skin is another theme set over the same names: each vendored set must define all of them."""
    for name in load_runtime().names.theme_css:
        theme = name.removeprefix("onex.theme.").removesuffix(".css")
        values = _theme_values((VENDOR_DIR / name).read_text(encoding="utf-8"), theme)
        for target in ALIASES.values():
            assert target in values, (name, target)


def test_aliased_names_leave_the_dark_rules_and_nothing_else_is_aliased() -> None:
    dark_rules = PAGE[
        PAGE.index("@media (prefers-color-scheme: dark)") : PAGE.index("*{box-sizing")
    ]
    for name in ALIASES:
        assert f"{name}:" not in dark_rules, name
    assert set(_root_aliases(PAGE)) == set(ALIASES)


def test_page_links_exactly_the_published_theme_files_and_bridges_data_theme() -> None:
    names = load_runtime().names.theme_css
    hrefs = re.findall(r'<link rel="stylesheet" href="([^"]+)">', PAGE)
    assert tuple(hrefs) == names
    assert 'data-onex-theme="onex.theme.light"' in PAGE.split(">", 2)[1] + ">"
    bridge = re.search(r'<script id="theme-bridge">(.*?)</script>', PAGE, re.DOTALL)
    assert bridge is not None
    assert 'getAttribute("data-theme")' in bridge.group(1)
    assert '"onex.theme."' in bridge.group(1)
    # The bridge script is not the page's main script: tests and tools that read the bare <script> keep working.
    assert PAGE.count("<script>") == 1


def test_a_declared_theme_name_changes_what_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = load_runtime()
    declared: Any = runtime.model_copy(
        update={
            "names": runtime.names.model_copy(
                update={"theme_css": ("onex.theme.warm.css",)}
            )
        }
    )
    monkeypatch.setattr(handler_module, "load_runtime", lambda: declared)
    result = HandlerFactoryFloorPublish(LocalSsh()).handle(
        _request(tmp_path), environ={}
    )
    assert result.published[0] == "onex.theme.warm.css"
    assert "onex.theme.light.css" not in result.published
    assert (tmp_path / "site/onex.theme.warm.css").read_bytes() == (
        VENDOR_DIR / "onex.theme.warm.css"
    ).read_bytes()
