# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plain customer-visible messages (OMN-26620 AC9).

A raw exception, errno, path or environment name never reaches the screen:
it is logged here and a short sentence plus a next step is returned instead.
"""

from __future__ import annotations

import logging
from typing import Any

_LOG = logging.getLogger(__name__)


def _design() -> Any:
    try:
        from .models.model_tui_design import tui_design

        return tui_design()
    except Exception:
        return None


def _str_field(obj: Any, name: str, default: str) -> str:
    if obj is None:
        return default
    value = getattr(obj, name, None)
    if isinstance(value, str) and value:
        return value
    if isinstance(obj, dict):
        value = obj.get(name)
        if isinstance(value, str) and value:
            return value
    return default


def plain_message(
    raw: Any = None,
    next_step: str | None = None,
    what: str | None = None,
    **kwargs: Any,
) -> str:
    """Return a plain sentence plus a next step, logging ``raw`` to the log.

    ``raw`` (an exception, errno, path, or command output) is never placed
    in the returned string; it is written to the log only.
    """
    if next_step is None:
        next_step = kwargs.get("next_step")
    if what is None:
        what = kwargs.get("what", "")
    what = what or ""

    design = _design()
    messages = getattr(design, "messages", None) if design is not None else None
    could_not = _str_field(messages, "could_not", "Could not {what}.")
    default_next = _str_field(messages, "next_step", "Run /doctor.")
    if next_step is None:
        next_step = default_next

    try:
        sentence = (
            could_not.format(what=what)
            if what
            else could_not.replace("{what}", "").strip()
        )
    except (KeyError, ValueError, IndexError):
        sentence = could_not
    sentence = sentence.strip() or "Could not finish that."
    sentence = sentence.rstrip(". ") + "."

    _LOG.warning(
        "raw customer-visible message hidden: %r",
        raw,
        exc_info=isinstance(raw, BaseException),
    )

    return f"{sentence} {next_step}".strip()


# Aliases kept so callers (and tests) can use any of the likely names.
plain_problem = plain_message
plain_customer_message = plain_message
plain_reason_safe = plain_message


class PlainMessage:
    """Object form, for code that prefers a small helper instance."""

    @staticmethod
    def render(
        raw: Any = None, *, next_step: str | None = None, what: str | None = None
    ) -> str:
        return plain_message(raw, next_step=next_step, what=what)


def hide(
    raw: Any = None, *, next_step: str | None = None, what: str | None = None
) -> str:
    return plain_message(raw, next_step=next_step, what=what)
