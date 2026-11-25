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

EnumTuiAction = Literal[
    "config",
    "model",
    "delegation",
    "dashboard",
    "metering",
    "themes",
    "clear",
    "help",
    "quit",
]


def _colour(value: str) -> str:
    if not _COLOUR.match(value):
        raise ValueError(f"{value!r} is not a #rrggbb colour")
    return value


class ModelTuiPaint(BaseModel):
    """The three background colours the app registers, and whether the app
    draws with the terminal's own palette (the terminal's background shows)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    background: str
    surface: str
    panel: str
    ansi: bool


class ModelTuiTheme(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    dark: bool
    #: Whose background the screen shows: the terminal's own, or this theme's.
    ground: Literal["terminal", "black"] = "terminal"
    primary: str
    secondary: str
    accent: str
    success: str
    #: Information that is neither good nor bad: a cloud model, an available one.
    info: str
    warning: str
    error: str
    foreground: str
    muted: str
    background: str
    surface: str
    panel: str
    #: A faint lift over a surface, with alpha (#rrggbbaa).
    boost: str
    border: str
    #: The fill behind a question in the conversation.
    chip: str = "#2c313c"
    #: A task card's bar for a cheap model, and for the Claude fallback.
    route_cheap: str
    route_fallback: str

    @field_validator(
        "primary",
        "secondary",
        "accent",
        "success",
        "info",
        "warning",
        "error",
        "foreground",
        "muted",
        "background",
        "surface",
        "panel",
        "border",
        "chip",
        "route_cheap",
        "route_fallback",
    )
    @classmethod
    def _is_colour(cls, value: str) -> str:
        return _colour(value)

    @field_validator("boost")
    @classmethod
    def _is_boost(cls, value: str) -> str:
        if not re.fullmatch(r"#[0-9a-fA-F]{8}", value):
            raise ValueError(f"{value!r} is not a #rrggbbaa colour")
        return value

    def paint(self, ground: str | None = None) -> ModelTuiPaint:
        """The backgrounds to register: none of our own when the terminal's shows."""
        if (ground or self.ground) == "terminal":
            return ModelTuiPaint(
                background="ansi_default",
                surface="ansi_default",
                panel="ansi_default",
                ansi=True,
            )
        return ModelTuiPaint(
            background=self.background,
            surface=self.surface,
            panel=self.panel,
            ansi=False,
        )


class ModelTuiWordmark(BaseModel):
    """The ONEX wordmark: the same number of rows, all of one width."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[str, ...] = Field(min_length=2, max_length=2)
    style: str

    @model_validator(mode="after")
    def _rows_align(self) -> ModelTuiWordmark:
        if len({len(row) for row in self.rows}) != 1:
            raise ValueError("wordmark rows differ in width")
        return self


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
    dot_on: str = Field(min_length=1)
    dot_off: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    cancelled: str = "⊘"


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


class ModelTuiLayoutColumn(BaseModel):
    """One column of a tab table (OMN-26620): what it shows and its title."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: Literal[
        "pick",
        "model",
        "model_marked",
        "model_numbered",
        "context",
        "price_in",
        "price_out",
        "capabilities",
        "status",
    ]
    title: str


class ModelTuiLayout(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    header: bool
    #: Show the search box above the table (the Config tab's found list).
    search: bool = False
    columns: tuple[ModelTuiLayoutColumn, ...] = Field(min_length=1)


class ModelTuiTabTable(BaseModel):
    """The words and layouts of the tables on the Config and Model tabs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticked: str = Field(min_length=1)
    unticked: str = Field(min_length=1)
    marker_first: str = Field(min_length=1)
    marker_other: str = Field(min_length=1)
    context_thousand: str = Field(min_length=1)
    context_million: str = Field(min_length=1)
    capability_join: str = Field(min_length=1)
    status: dict[str, ModelTuiWord]
    layouts: dict[str, ModelTuiLayout]


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
    #: After a row's provider, once that provider's kept list has expired (AC4).
    listing_expired: str = Field(min_length=1)
    #: The line above the table: how many shown, the filter and the sort.
    bar: str
    #: Added to the bar where rows can be ticked.
    bar_ticked: str
    auto_label: str
    help: dict[str, str]
    #: The tables on the Config and Model tabs (OMN-26620 mockups pages 4 and 5).
    tab_table: ModelTuiTabTable


class ModelTuiDelegationHook(BaseModel):
    """One delegation hook as /delegation shows it (OMN-23413)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Its evidence key in `onex delegation status --json` (claude-hooks step).
    key: Literal["prompt_hook"]
    #: The switch's own label (operator 2026-11-15: one switch per hook).
    label: str
    name: str
    what: str
    #: Its key in status's "switches" (~/.onex/delegation.json), and the
    #: `onex delegation on` flag that pauses it (operator 2026-11-15).
    switch: Literal["prompts"]
    pause_flag: str = Field(pattern=r"^--no-[a-z]+$")


class ModelTuiDelegation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    table_title: str
    enable: tuple[tuple[str, ...], ...] = Field(min_length=1)
    disable: tuple[tuple[str, ...], ...] = Field(min_length=1)
    switched_on: str
    switched_off: str
    not_switched: str
    nothing_ticked: str
    no_models: str
    glyph_on: str = Field(min_length=1)
    glyph_off: str = Field(min_length=1)
    state_on: str
    state_off: str
    state_pending: str
    #: A state not read yet, or whose read failed: never drawn as OFF.
    glyph_unknown: str = Field(min_length=1)
    state_reading: str
    state_unread: str
    hooks_title: str
    #: One hook, the prompt hook (OMN-23413 Amendment 14a, 2026-11-17).
    hooks: tuple[ModelTuiDelegationHook, ...] = Field(min_length=1, max_length=1)
    hook_installed: str
    hook_missing: str
    hook_unknown: str
    hook_unread: str
    hook_paused: str
    #: What an untouched Save says when the state was not read (it sends nothing).
    save_reading: str
    save_unread: str
    save_just_read: str
    #: OMN-23413: setup's read-only check of the claude-plugin step, run when the
    #: tab opens; its evidence.claude_code says installed, signed out or missing.
    claude_check: tuple[str, ...] = Field(min_length=1)


class ModelTuiDashboard(BaseModel):
    """/dashboard (OMN-23413): the local dashboard's state and the onex commands
    that start, open and stop it; the screen holds none of them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    status: tuple[str, ...] = Field(min_length=1)
    start: tuple[str, ...] = Field(min_length=1)
    open: tuple[str, ...] = Field(min_length=1)
    stop: tuple[str, ...] = Field(min_length=1)
    running: str
    not_running: str
    started: str
    opened: str
    stopped: str
    failed: str
    state_running: str
    state_running_nopid: str
    state_stopped: str
    state_reading: str
    state_unread: str
    dot_on: str = Field(min_length=1)
    dot_off: str = Field(min_length=1)
    dot_reading: str = Field(min_length=1)
    button_open: str
    note_opened: str
    could_not: str
    could_not_read: str


class ModelTuiActivity(BaseModel):
    """The live line while a task runs, as coding agents' CLIs show (OMN-23413)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Spinner frames, shown in turn every ``frame_seconds``.
    frames: tuple[str, ...] = Field(min_length=2)
    frame_seconds: float = Field(gt=0)
    #: Words for what is happening, shown in turn every ``verb_seconds``.
    verbs: tuple[str, ...] = Field(min_length=1)
    verb_seconds: float = Field(gt=0)
    #: ``{frame}``, ``{verb}`` and ``{seconds}`` are filled in.
    line: str = Field(min_length=1)
    #: The task's path drawn after the line, a dot travelling it; ``{model}``
    #: is the model tried first, or ``flow_auto`` when Auto picks.
    flow: tuple[str, ...] = ()
    flow_auto: str = "best model"
    #: Steps between two stops of the flow, the arrow included.
    flow_gap: int = Field(default=6, ge=2)


class ModelTuiHeader(BaseModel):
    """Compact header on the start screen (OMN-26620)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: tuple[str, ...] = Field(min_length=1, max_length=4)
    version: str = Field(min_length=1)
    packages: tuple[str, ...] = Field(min_length=1)
    home: str = Field(min_length=1)
    ellipsis: str = Field(min_length=1)
    compact: bool = True


class ModelTuiTaskCard(BaseModel):
    """A delegated task rendered as a card (OMN-26620)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state_bar_colour: str
    facts_template: str = Field(min_length=1)
    fallback_facts_template: str = Field(
        default="{model} · {tokens} tokens · {seconds} s", min_length=1
    )
    failure_template: str = Field(
        default="Could not reach {model}. Check its key with /config.", min_length=1
    )
    glyphs: dict[str, str] = Field(default_factory=dict)
    bar_colours: dict[str, str] = Field(default_factory=dict)

    @property
    def state_glyphs(self) -> dict[str, str]:
        return self.glyphs

    @property
    def state_bar_colours(self) -> dict[str, str]:
        return self.bar_colours

    @property
    def state_colors(self) -> dict[str, str]:
        return self.bar_colours


class ModelTuiSpinner(BaseModel):
    """A non-blocking spinner (OMN-26620 AC11)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    frames: tuple[str, ...] = Field(default_factory=lambda: tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"))
    fallback_frames: tuple[str, ...] = ("|", "/", "-", "\\")
    ascii_frames: tuple[str, ...] = ("|", "/", "-", "\\")
    frame_seconds: float = Field(default=0.1, gt=0)
    text: str = "Thinking… (ctrl+c to interrupt)"
    wording: str = "Thinking… (ctrl+c to interrupt)"
    message: str = "Thinking… (ctrl+c to interrupt)"
    line: str = "Thinking… (ctrl+c to interrupt)"


class ModelTuiFooterKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    #: What the key does, in the muted words after it ("" for a bare label).
    words: str = ""


class ModelTuiFooterScreen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    keys: tuple[ModelTuiFooterKey, ...] = Field(min_length=1)
    #: Keys to drop first when the line is too narrow, first dropped first.
    drop_order: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _drops_only_its_own_keys(self) -> ModelTuiFooterScreen:
        labels = {key.label for key in self.keys}
        for label in self.drop_order:
            if label not in labels:
                raise ValueError(f"drop order names {label!r}, which is not a key")
        return self


class ModelTuiFooter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Between two hints on one line (a middle dot).
    separator: str = Field(min_length=1)
    screens: dict[str, ModelTuiFooterScreen] = Field(default_factory=dict)


class ModelTuiIcon(BaseModel):
    """One icon in every form: Nerd Font, Unicode (the default), ASCII."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nerd: str = Field(min_length=1)
    fallback: str = Field(min_length=1)
    ascii: str = Field(min_length=1)

    @field_validator("ascii")
    @classmethod
    def _is_ascii(cls, value: str) -> str:
        if not value.isascii():
            raise ValueError(f"{value!r} is not plain ASCII")
        return value


class ModelTuiIcons(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["nerd", "plain", "ascii"] = "plain"
    enabled: bool = False
    glyphs: dict[str, ModelTuiIcon] = Field(default_factory=dict)


class ModelTuiPalette(BaseModel):
    """What the / palette and ctrl+p show for the customer commands."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name_width: int = Field(ge=8)
    themes_title: str = Field(min_length=1)
    themes_help: str = Field(min_length=1)
    icon_commands: tuple[str, ...]
    hints: dict[str, str]


class ModelTuiTabSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: EnumTuiAction
    label: str = Field(min_length=1)


class ModelTuiDoctorPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    title: str = Field(default="Setup check", min_length=1)
    summary_all_good: str = Field(
        default="✓ all good · {passed} of {total}", min_length=1
    )
    summary_attention: str = Field(
        default="{problems} need attention · {passed} of {total}", min_length=1
    )
    status_ok: str = Field(default="✓ ok", min_length=1)
    status_attention: str = Field(default="! needs attention", min_length=1)
    status_skipped: str = Field(default="– optional/skipped", min_length=1)  # noqa: RUF001
    fix_prefix: str = Field(default="fix:", min_length=1)


class ModelTuiMeteringPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    title: str = Field(default="Usage and cost", min_length=1)
    tasks: str = Field(default="Tasks", min_length=1)
    tokens: str = Field(default="Tokens", min_length=1)
    cost: str = Field(default="Cost", min_length=1)
    saved: str = Field(default="Saved ≈ {amount} (modelled)", min_length=1)
    unmeasured: str = Field(default="not measured", min_length=1)
    savings_line: str = Field(default="Savings are modelled, not billed.", min_length=1)
    period: str = Field(default="{period}", min_length=1)


class ModelTuiMessages(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    could_not: str = Field(default="Could not {what}.", min_length=1)
    next_step: str = Field(default="Run /doctor.", min_length=1)


class ModelTuiDiagram(BaseModel):
    """The routing diagram on the welcome screen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    heading: str = Field(min_length=1)
    task: str = Field(min_length=1)
    hub_title: str = Field(min_length=1)
    hub_subtitle: str = Field(min_length=1)
    hub_tagline: str = Field(min_length=1)
    outcome_icons: tuple[str, str, str]
    outcomes: tuple[str, str, str]
    pulse: bool = True
    pulse_seconds: float = Field(default=0.3, gt=0.05, le=2.0)


class ModelTuiGuideStep(BaseModel):
    """One box of the Setup Guide: a tab and what it is for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z]+$")
    key: str = Field(pattern=r"^[0-9]$")
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ModelTuiGuide(BaseModel):
    """The Setup Guide: a static welcome guide to the four tabs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1)
    min_height: int = Field(ge=30)
    steps: tuple[ModelTuiGuideStep, ...] = Field(min_length=4, max_length=4)


class ModelTuiWelcome(BaseModel):
    """The welcome screen of a roomy terminal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_width: int = Field(ge=60)
    min_height: int = Field(ge=20)
    headline: str = Field(min_length=1)
    subline: str = Field(min_length=1)
    brand: str = Field(min_length=1)
    brand_rule: str = Field(min_length=1)
    pixel: str = Field(min_length=1)
    letter_gap: int = Field(ge=0, le=6)
    order: str = Field(min_length=1)
    diagram: ModelTuiDiagram
    guide: ModelTuiGuide
    letter_styles: tuple[str, ...]
    letters: dict[str, tuple[str, ...]]

    @model_validator(mode="after")
    def _letters_are_complete(self) -> ModelTuiWelcome:
        if len(self.letter_styles) != len(self.order):
            raise ValueError("letter_styles needs one style per letter of order")
        heights: set[int] = set()
        widths: set[int] = set()
        for char in self.order:
            rows = self.letters.get(char)
            if not rows:
                raise ValueError(f"letter {char!r} has no pixel rows")
            heights.add(len(rows))
            widths.update(len(row) for row in rows)
        if len(heights) != 1 or len(widths) != 1:
            raise ValueError("the letters differ in height or width")
        return self


class ModelTuiDesign(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    theme: ModelTuiTheme
    #: Optional additional themes selectable via /theme (OMN-26620 AC5).
    themes: dict[str, ModelTuiTheme] = Field(default_factory=dict)
    header: ModelTuiHeader
    welcome: ModelTuiWelcome
    task_card: ModelTuiTaskCard
    footer: ModelTuiFooter
    doctor_page: ModelTuiDoctorPage = Field(default_factory=ModelTuiDoctorPage)
    metering_page: ModelTuiMeteringPage = Field(default_factory=ModelTuiMeteringPage)
    messages: ModelTuiMessages = Field(default_factory=ModelTuiMessages)
    icons: ModelTuiIcons = Field(default_factory=ModelTuiIcons)
    palette: ModelTuiPalette
    wordmark: ModelTuiWordmark
    shortcuts: tuple[ModelTuiShortcut, ...] = Field(min_length=1)
    aliases: dict[str, str]
    #: CLI command paths a shortcut replaces: the TUI neither lists nor runs them.
    replaced_commands: tuple[tuple[str, ...], ...] = ()
    #: The only commands `/` lists and the TUI runs, in this order; every other
    #: CLI command stays in the CLI; the TUI has no list-everything switch.
    customer_commands: tuple[tuple[str, ...], ...] = Field(min_length=1)
    model_table: ModelTuiModelTable
    delegation: ModelTuiDelegation
    dashboard: ModelTuiDashboard
    activity: ModelTuiActivity
    spinner: ModelTuiSpinner = Field(default_factory=ModelTuiSpinner)
    tabs: tuple[ModelTuiTabSpec, ...] = Field(min_length=1)
    #: The model screens' sentences, by name; ``{name}`` parts are filled in.
    words: dict[str, str]
    glyphs: ModelTuiGlyphs
    prompt_placeholder: str
    help: str
    auto_find_seconds: float = Field(gt=0)
    #: How long a Done or Failed pop-up stays on screen.
    toast_seconds: float = Field(gt=0)
    #: A failure reason longer than this is cut on screen.
    reason_chars: int = Field(ge=20)
    #: A setup read this recent, and not shown yet, opens a tab as it is.
    fresh_seconds: float = Field(gt=0)
    min_key_length: int = Field(ge=1)
    #: /config names the models set up while that line fits in this many
    #: characters; past it, a count per provider (OMN-23413).
    basket_names_max_chars: int = Field(ge=40)

    @model_validator(mode="after")
    def _draws_correctly(self) -> ModelTuiDesign:
        names = {s.path for s in self.shortcuts}
        if len(names) != len(self.shortcuts):
            raise ValueError("two shortcuts share a path")
        if len(set(self.customer_commands)) != len(self.customer_commands):
            raise ValueError("two customer commands share a path")
        for path in self.customer_commands:
            if self.replaces(path) and path not in names:
                raise ValueError(f"customer command {path} is a replaced command")
        for alias, target in self.aliases.items():
            if (target,) not in names:
                raise ValueError(f"alias {alias!r} names no shortcut {target!r}")
            if (alias,) in self.replaced_commands:
                raise ValueError(f"alias {alias!r} is a replaced command")
        for path in self.customer_commands:
            if path[0] not in self.palette.hints:
                raise ValueError(f"palette has no hint for {path[0]!r}")
        for name in self.palette.icon_commands:
            if f"cmd_{name}" not in self.icons.glyphs:
                raise ValueError(f"palette command {name!r} has no icon")
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
            if wanted not in table.tab_table.status:
                raise ValueError(f"tab_table status has no word for {wanted!r}")
        for name, shown in table.tab_table.status.items():
            if shown.colour not in tokens:
                raise ValueError(f"tab_table word {name!r}: no colour {shown.colour!r}")
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
