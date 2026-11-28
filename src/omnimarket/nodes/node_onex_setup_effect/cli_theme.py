# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26620 AC5: resolve TUI text colours from the active theme.

Colours for TUI text are drawn from the theme on screen: an onex theme's own
tokens from tui_design.yaml, or a Textual built-in theme's colours under the
same names. Text built with no TUI running uses the default theme.
"""

from __future__ import annotations

from typing import Any

from textual._context import active_app
from textual.app import App
from textual.color import Color, ColorParseError
from textual.theme import Theme

from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
    ModelTuiTheme,
    tui_design,
)

#: info and boost have no Textual variable so a built-in theme keeps the default's.
_BUILT_IN: dict[str, str] = {
    "primary": "primary",
    "secondary": "secondary",
    "accent": "accent",
    "success": "success",
    "warning": "warning",
    "error": "error",
    "foreground": "foreground",
    "background": "background",
    "surface": "surface",
    "panel": "panel",
    "border": "border",
    "chip": "chip",
    "route_cheap": "route-cheap",
    "route_fallback": "route-fallback",
}

#: Textual's own $foreground-muted is the foreground at this alpha, i.e. how much of the foreground muted text keeps over the background.
_MUTED: float = 0.6

_BUILT: dict[tuple[int, str], ModelTuiTheme] = {}


def _plain(value: str | None) -> Color | None:
    """A colour Rich can draw, or None for an ANSI name or a relative value."""
    if value is None:
        return None
    try:
        color = Color.parse(value)
    except ColorParseError:
        return None
    if color.ansi is not None:
        return None
    return color


def _from_built_in(theme: Theme, default: ModelTuiTheme) -> ModelTuiTheme:
    """The default tokens, each one the Textual theme defines replaced by its colour."""
    variables: dict[str, str] = theme.to_color_system().generate()
    variables.update(theme.variables)
    updates: dict[str, str] = {}
    for token, variable in _BUILT_IN.items():
        colour = _plain(variables.get(variable))
        if colour is not None:
            updates[token] = colour.hex6
    background = _plain(variables.get("background"))
    foreground = _plain(variables.get("foreground"))
    if background is not None and foreground is not None:
        updates["muted"] = background.blend(foreground, _MUTED).hex6
    return default.model_copy(update=updates)


def colours_for(app: App[Any]) -> ModelTuiTheme:
    """The colour tokens of ``app``'s theme; the default for an app that is not
    the onex TUI (it has not registered the onex themes)."""
    design = tui_design()
    default = design.theme
    if default.name not in app.available_themes:
        return default
    name = str(app.theme)
    if name == default.name:
        return default
    if name in design.themes:
        return design.themes[name]
    key = (id(app), name)
    if key not in _BUILT:
        _BUILT.clear()  # one app draws at a time; a stale one is never served
        _BUILT[key] = _from_built_in(app.current_theme, default)
    return _BUILT[key]


def active_colours() -> ModelTuiTheme:
    """The colour tokens of the TUI drawing now, or the default with none running."""
    app = active_app.get(None)
    if app is None:
        return tui_design().theme
    return colours_for(app)


def style(token: str, *extra: str) -> str:
    """
    A Rich style from a design colour token in the theme on screen, e.g.
    ``style("error", "bold")``.
    """
    return " ".join((*extra, str(getattr(active_colours(), token))))
