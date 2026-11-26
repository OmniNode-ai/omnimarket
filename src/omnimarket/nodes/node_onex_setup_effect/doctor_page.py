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
import logging
from collections.abc import Mapping
from typing import Any

_LOG = logging.getLogger(__name__)


def _design() -> Any:
    try:
        from .models.model_tui_design import tui_design

        return tui_design()
    except Exception:  # pragma: no cover
        return None


def _page() -> Any:
    design = _design()
    return getattr(design, "doctor_page", None) if design is not None else None


def _g(key: str, default: str) -> str:
    page = _page()
    if page is None:
        return default
    value = getattr(page, key, None)
    if isinstance(value, str) and value:
        return value
    if isinstance(page, Mapping):
        value = page.get(key)
        if isinstance(value, str) and value:
            return value
    return default


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
        return text or "ok"
    if unsafe or not text:
        # the row's mark already says needs attention; do not say it twice
        return "" if status in ("unhealthy", "degraded") else "not set up"
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


def _mark_for(status: str) -> str:
    key = {
        "healthy": "status_ok",
        "unhealthy": "status_attention",
        "degraded": "status_attention",
        "unknown": "status_skipped",
    }.get(status, "status_skipped")
    defaults = {
        "status_ok": "✓ ok",
        "status_attention": "! needs attention",
        "status_skipped": "– optional/skipped",  # noqa: RUF001
    }
    return _g(key, defaults[key])


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

    title = _g("title", "Setup check")
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

    if failed == 0 and total:
        template = _g("summary_all_good", "✓ all good · {passed} of {total}")
        try:
            summary = template.format(passed=passed, total=total, problems=failed)
        except (KeyError, ValueError, IndexError):
            summary = f"✓ all good · {passed} of {total}"
    else:
        template = _g(
            "summary_attention", "{problems} need attention · {passed} of {total}"
        )
        try:
            summary = template.format(passed=passed, total=total, problems=failed)
        except (KeyError, ValueError, IndexError):
            summary = f"{failed} need attention · {passed} of {total}"

    # Pad title and summary apart like the mockup.
    gap = max(1, width - len(title) - len(summary))
    lines: list[str] = [title + (" " * gap) + summary]

    fix_prefix = _g("fix_prefix", "fix:").rstrip()
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
        sentence = _coded(check.get("message")) or _safe_sentence(
            check.get("message"), status
        )
        # Only a fix this page can name: /config cannot start Docker.
        fix = fixes.get(raw_name, "") if status != "healthy" else ""
        rows.append((_mark_for(status), name, sentence, fix))

    # One column each for the mark, the name and the sentence.
    mark_width = max((len(mark) for mark, _, _, _ in rows), default=0)
    name_width = max((len(name) for _, name, _, _ in rows), default=0)
    for mark, name, sentence, fix in rows:
        said = "  ".join(
            part for part in (sentence, f"{fix_prefix} {fix}" if fix else "") if part
        )
        row = f"{mark:<{mark_width}}  {name:<{name_width}}  {said}".rstrip()
        lines.append(row)

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
