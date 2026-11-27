# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The /doctor page (OMN-26620).

Reads the `onex doctor --json` output and renders one row per check with a
status mark, its name and a plain sentence. No fault code, credential or
environment name, path, or exception text reaches the screen; the raw JSON is
logged instead.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Mapping
from typing import Any

from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
    ModelTuiDoctorPage,
    tui_design,
)


def _page() -> ModelTuiDoctorPage:
    return tui_design().doctor_page


def _g(key: str) -> str:
    """One of the page's words from tui_design.yaml (AC8: none is written here)."""
    return str(getattr(_page(), key))


def _coerce(payload: Any) -> Mapping[str, Any] | None:
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return None
        try:
            value = json.loads(text)
            if isinstance(value, Mapping):
                return value
        except (ValueError, TypeError):
            # Non-JSON text falls through to the line-by-line loop below.
            pass
        # Last JSON object in mixed output.
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    value = json.loads(line)
                    if isinstance(value, Mapping):
                        return value
                except (ValueError, TypeError):
                    continue
        return None
    return None


def _safe_sentence(message: Any, status: str) -> str:
    text = "" if message is None else str(message).strip()
    lowered = text.lower()
    # Environment names, paths and fault codes never reach the screen.
    unsafe = (
        "/" in text
        or "\\" in text
        or ("_" in text and text.isascii() and any(ch.isupper() for ch in text))
        or (":" in text and any(ch.isdigit() for ch in text))
        or "missing" in lowered
        or ("skipped" in lowered and "_" in text)
    )
    if status == "healthy":
        return text or _g("said_ok")
    if unsafe or not text:
        # the row's mark already says needs attention; do not say it twice
        return "" if status in ("unhealthy", "degraded") else _g("said_not_set_up")
    return text


def _table(key: str) -> Mapping[str, str]:
    """A mapping from the page's design (fixes, codes), or empty."""
    value = getattr(_page(), key, None)
    return value if isinstance(value, Mapping) else {}


def _coded(message: Any) -> str | None:
    """The plain sentence for a message that opens with a "[code]" the page knows."""
    text = "" if message is None else str(message).strip()
    if not text.startswith("["):
        return None
    code = text[1 : text.find("]")] if "]" in text else ""
    return _table("codes").get(code)


def _started(message: Any) -> str | None:
    """The plain sentence for a message that starts the way the page knows."""
    text = "" if message is None else str(message).strip()
    for start, sentence in _table("starts").items():
        if text.startswith(start):
            return sentence
    return None


def _missing(message: Any) -> str | None:
    """ "Missing: A, B" said with each setting's plain name, or None when the
    page knows none of them."""
    text = "" if message is None else str(message).strip()
    if not text.lower().startswith("missing:"):
        return None
    known = _table("settings")
    names = [
        known[name]
        for name in (part.strip() for part in text.split(":", 1)[1].split(","))
        if name in known
    ]
    if not names:
        return None
    return _g("missing").format(names=", ".join(names))


def _sentence(message: Any, status: str, *, has_fix: bool = False) -> str:
    """What the row says: a known code, start or setting first, then the
    message itself when it is safe, and never nothing for a row that needs
    attention and names no fix (verify pass 2026-11-26)."""
    said = (
        _coded(message)
        or _started(message)
        or _missing(message)
        or _safe_sentence(message, status)
    )
    if not said and not has_fix and status in ("unhealthy", "degraded"):
        return _g("fallback")
    return said


def _mark_for(status: str) -> str:
    key = {
        "healthy": "status_ok",
        "unhealthy": "status_attention",
        "degraded": "status_attention",
        "unknown": "status_skipped",
    }.get(status, "status_skipped")
    return _g(key)


def render_doctor_page(
    payload: Any = None,
    *,
    text: str | None = None,
    width: int = 140,
    raw: Any = None,
) -> list[str]:
    """Return the /doctor page as a list of text lines."""
    report = _coerce(payload if payload is not None else text)
    from .plain_messages import plain_message

    title = _g("title")
    if report is None:
        sentence = plain_message(raw, what="read your setup", next_step="Run /doctor.")
        return [f"{title}  {sentence}"]

    try:
        total = int(report.get("total", len(report.get("checks", []))))
    except (TypeError, ValueError):
        total = len(report.get("checks", []))
    try:
        passed = int(report.get("passed", 0))
    except (TypeError, ValueError):
        passed = 0
    try:
        failed = int(report.get("failed", 0))
    except (TypeError, ValueError):
        failed = 0

    template = _g("summary_all_good" if failed == 0 and total else "summary_attention")
    summary = template.format(passed=passed, total=total, problems=failed)

    # Pad title and summary apart like the mockup.
    gap = max(1, width - len(title) - len(summary))
    lines: list[str] = [title + (" " * gap) + summary]

    fix_prefix = _g("fix_prefix").rstrip()
    fixes = _table("fixes")

    rows: list[tuple[str, str, str, str]] = []
    for check in report.get("checks", []) or []:
        if not isinstance(check, Mapping):
            continue
        status = str(check.get("status", "")).lower()
        raw_name = str(check.get("name", "")).strip()
        name = raw_name
        if "_" in name:  # a raw identifier becomes plain words
            name = name.replace("_", " ").capitalize()
        # Only a fix this page can name: /config cannot start Docker.
        fix = fixes.get(raw_name, "") if status != "healthy" else ""
        sentence = _sentence(check.get("message"), status, has_fix=bool(fix))
        rows.append((_mark_for(status), name, sentence, fix))

    # One column each for the mark, the name and the sentence.
    mark_width = max((len(mark) for mark, _, _, _ in rows), default=0)
    name_width = max((len(name) for _, name, _, _ in rows), default=0)
    # A row too long for the width wraps under its own sentence, not under
    # the marks (verify pass 2026-11-26, 80 columns).
    indent = mark_width + 2 + name_width + 2
    room = width - indent
    for mark, name, sentence, fix in rows:
        # A no-break space keeps "fix: /config" in one piece when it wraps.
        fix_said = f"{fix_prefix}\u00a0{fix}" if fix else ""
        said = "  ".join(part for part in (sentence, fix_said) if part)
        head = f"{mark:<{mark_width}}  {name:<{name_width}}  "
        if len(head) + len(said) <= width or room < 20:
            lines.append((head + said).replace("\u00a0", " ").rstrip())
            continue
        pieces = [
            piece.replace("\u00a0", " ")
            for piece in textwrap.wrap(said, room, break_on_hyphens=False)
        ] or [""]
        lines.append((head + pieces[0]).rstrip())
        lines.extend(" " * indent + piece for piece in pieces[1:])

    return lines


class DoctorPage:
    @staticmethod
    def render(
        payload: Any = None,
        *,
        text: str | None = None,
        width: int = 140,
        raw: Any = None,
    ) -> list[str]:
        return render_doctor_page(payload, text=text, width=width, raw=raw)

    @staticmethod
    def parse(text: str) -> Any:
        return _coerce(text)
