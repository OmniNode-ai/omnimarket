# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The onex TUI's design system, read from tui_design.yaml (OMN-23413).

A file that would draw wrongly is refused when it loads, not when a screen
shows it: a banner letter the font lacks, a gradient no word can find, a
colour that is not ``#rrggbb``, an alias that names no shortcut.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")

EnumTuiAction = Literal["config", "model", "delegation", "clear", "help", "quit"]


def _colour(value: str) -> str:
    if not _COLOUR.match(value):
        raise ValueError(f"{value!r} is not a #rrggbb colour")
    return value


class ModelTuiTheme(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    dark: bool
    primary: str
    secondary: str
    accent: str
    success: str
    warning: str
    error: str
    foreground: str
    muted: str
    background: str
    surface: str
    panel: str

    @field_validator(
        "primary",
        "secondary",
        "accent",
        "success",
        "warning",
        "error",
        "foreground",
        "muted",
        "background",
        "surface",
        "panel",
    )
    @classmethod
    def _is_colour(cls, value: str) -> str:
        return _colour(value)


class ModelTuiBannerWord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    gradient: str


class ModelTuiTip(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    text: str


class ModelTuiBanner(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    greeting: str = ""  # small italic line above the words; empty for none
    words: tuple[ModelTuiBannerWord, ...] = Field(min_length=1)
    tagline: str
    tips: tuple[ModelTuiTip, ...]


class ModelTuiShortcut(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: tuple[str, ...] = Field(min_length=1)
    action: EnumTuiAction
    help: str


class ModelTuiGlyphs(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str = Field(min_length=1)
    tip: str = Field(min_length=1)
    done: str = Field(min_length=1)
    unchanged: str = Field(min_length=1)
    failed: str = Field(min_length=1)


class ModelTuiTableColumn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: Literal[
        "pick", "provider", "model", "context", "price", "capabilities", "status"
    ]
    title: str


class ModelTuiWord(BaseModel):
    """A word shown in the table and the theme colour token it is drawn in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    colour: str = Field(min_length=1)


class ModelTuiModelTable(BaseModel):
    """The one model table /config, /model and /delegation all show (OMN-23413)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    columns: tuple[ModelTuiTableColumn, ...] = Field(min_length=1)
    price_local: str
    server_label: str
    details: dict[str, str]
    unknown_mark: str = Field(min_length=1)
    price_unknown: str
    price_variable: str
    price_tier_mark: str
    ticked: str = Field(min_length=1)
    unticked: str = Field(min_length=1)
    #: Leads every row, in the colour of the row's deployment.
    row_bar: str = Field(min_length=1)
    #: Leads every status word, in the status's colour.
    status_pill: str = Field(min_length=1)
    status: dict[str, ModelTuiWord]
    deployment: dict[str, ModelTuiWord]
    capability_colours: tuple[str, ...] = Field(min_length=1)
    filters: tuple[str, ...] = Field(min_length=1)
    filter_labels: dict[str, str]
    sorts: tuple[str, ...] = Field(min_length=1)
    sort_labels: dict[str, str]
    search_placeholder: str
    auto_label: str
    help: dict[str, str]


class ModelTuiDelegationMode(BaseModel):
    """One way to delegate Claude Code work, and the onex commands that switch it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    label: str
    detail: str
    needs_gateway_model: bool
    enable: tuple[tuple[str, ...], ...] = Field(min_length=1)
    disable: tuple[tuple[str, ...], ...] = Field(min_length=1)


class ModelTuiDelegation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    switch_label: str
    table_title: str
    modes: tuple[ModelTuiDelegationMode, ...] = Field(min_length=1)
    switched_on: str
    switched_off: str
    not_switched: str
    nothing_ticked: str
    connect: tuple[str, ...] = Field(min_length=1)
    no_models: str
    no_gateway_model: str
    gateway_marker: str


class ModelTuiTabSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: EnumTuiAction
    label: str = Field(min_length=1)


class ModelTuiDesign(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    theme: ModelTuiTheme
    gradients: dict[str, tuple[str, ...]]
    font: dict[str, tuple[str, str, str]]
    banner: ModelTuiBanner
    shortcuts: tuple[ModelTuiShortcut, ...] = Field(min_length=1)
    aliases: dict[str, str]
    #: CLI command paths a shortcut replaces: the TUI neither lists nor runs them.
    replaced_commands: tuple[tuple[str, ...], ...] = ()
    model_table: ModelTuiModelTable
    delegation: ModelTuiDelegation
    tabs: tuple[ModelTuiTabSpec, ...] = Field(min_length=1)
    #: The model screens' sentences, by name; ``{name}`` parts are filled in.
    words: dict[str, str]
    glyphs: ModelTuiGlyphs
    prompt_placeholder: str
    help: str
    auto_find_seconds: float = Field(gt=0)
    min_key_length: int = Field(ge=1)

    @model_validator(mode="after")
    def _draws_correctly(self) -> ModelTuiDesign:
        for name, ramp in self.gradients.items():
            if not ramp:
                raise ValueError(f"gradient {name!r} has no colours")
            for colour in ramp:
                _colour(colour)
        for word in self.banner.words:
            if word.gradient not in self.gradients:
                raise ValueError(
                    f"banner word {word.text!r}: no gradient {word.gradient!r}"
                )
            missing = sorted({c for c in word.text if c not in self.font})
            if missing:
                raise ValueError(f"banner word {word.text!r}: font lacks {missing}")
        for letter, rows in self.font.items():
            if len({len(row) for row in rows}) != 1:
                raise ValueError(f"font letter {letter!r}: rows differ in width")
        names = {s.path for s in self.shortcuts}
        if len(names) != len(self.shortcuts):
            raise ValueError("two shortcuts share a path")
        for alias, target in self.aliases.items():
            if (target,) not in names:
                raise ValueError(f"alias {alias!r} names no shortcut {target!r}")
            if (alias,) in self.replaced_commands:
                raise ValueError(f"alias {alias!r} is a replaced command")
        table = self.model_table
        tokens = set(ModelTuiTheme.model_fields)
        for name, shown in (*table.status.items(), *table.deployment.items()):
            if shown.colour not in tokens:
                raise ValueError(
                    f"model_table word {name!r}: no colour {shown.colour!r}"
                )
        for colour in table.capability_colours:
            if colour not in tokens:
                raise ValueError(
                    f"model_table capability colour {colour!r} is no token"
                )
        for wanted in ("ready", "listed", "untested", "failed", "unavailable"):
            if wanted not in table.status:
                raise ValueError(f"model_table status has no word for {wanted!r}")
        return self

    def replaces(self, path: tuple[str, ...]) -> bool:
        """Whether ``path`` is, or sits under, a CLI command a shortcut replaces."""
        return any(path[: len(cut)] == cut for cut in self.replaced_commands)

    def shortcut_for(self, words: list[str]) -> ModelTuiShortcut | None:
        """The shortcut typed words name, directly or through an alias."""
        if len(words) == 1 and words[0] in self.aliases:
            words = [self.aliases[words[0]]]
        return next((s for s in self.shortcuts if list(s.path) == words), None)


def load_tui_design(path: Path) -> ModelTuiDesign:
    return ModelTuiDesign.model_validate(yaml.safe_load(path.read_text()))


@cache
def tui_design() -> ModelTuiDesign:
    return load_tui_design(Path(__file__).resolve().parents[1] / "tui_design.yaml")
