# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The running-path cell of a TERMINAL row that confirms work.

Problem (fact). A TERMINAL could close a lane with ``outcome=done`` and evidence of
everything except the change itself: receipts, closeouts, host notes. Nothing in the row
named the entry point that runs the change, and nothing quoted a line from that entry
point's latest run, so ``done`` was a word no reader could follow to a running thing.

Rule. When ``ONEX_LEDGER_REQUIRE_RUNNING_PATH`` names a cutover (default OFF), a
TERMINAL stamped at or after it whose ``outcome=`` is ``done`` or ``handed-off`` carries

* ``running_path=<entry point that runs the change: unit, node, route or tick step>``
  with ``running_line=<one line from its latest run showing the changed behaviour>``,
  each one cell, or
* ``running_path=none-ci-only:<reason>`` — the honest cell for a change that lives only
  in CI and has no entry point a reader could run, with a reason matching
  ``^[a-z0-9][a-z0-9-]{3,80}$`` and no placeholder.

The value ``1`` (or true/yes/on) judges every row. Placeholder spellings (``none``,
``tbd``, ``x``, ...) are refused for both cells, a path is one token, and the line may
not be a copy of the path. Exempt, as for the delegation and worktree cells: a row whose
identity is ``actor=script:*``, ``actor=launchd:*`` or ``model=none`` — the grammar's
``_governed_terminal_row`` test, which the caller applies; a machine lane has no latest
run of its own to quote.

THE HONEST LIMIT. This judges shape only. It cannot tell that the quoted line is true,
nor that the named entry point is the one that executes the change; a lane can still
paste a line from an unrelated run. That half is the reading node and Fable review.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

ENFORCE_FROM_ENV = "ONEX_LEDGER_REQUIRE_RUNNING_PATH"

CONFIRMING_OUTCOMES = frozenset({"done", "handed-off"})
NONE_CI_ONLY = "none-ci-only"
PLACEHOLDERS = frozenset(
    {"", "-", "--", ".", "none", "n/a", "na", "nil", "tbd", "nothing", "?", "x"}
)
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9-]{3,80}$")
_CUTOVER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_TRUTHY = frozenset({"1", "true", "yes", "on"})
# The cutover that sorts before every real stamp, so the plain string comparison of
# fixed-width UTC stamps judges all of them.
EVERY_ROW_STAMP = "0000-00-00T00:00:00Z"

DECLARED_KEYS = frozenset({"outcome", "running_path", "running_line"})
_KEY_VALUE_RE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)=(\S*)")

SHAPE = (
    "running_path=<entry point that runs the change: unit, node, route or tick step> "
    "| running_line=<one line from its latest run showing the changed behaviour>, "
    "or running_path=none-ci-only:<reason>"
)


def enforced_from(environ: Mapping[str, str] | None = None) -> str | None:
    """The cutover stamp rows are judged from, or None when the rule is off.

    ``1``/``true``/``yes``/``on`` judges every row; a full UTC stamp judges rows
    stamped at or after it (both are fixed-width UTC stamps, so string order is
    time order); anything else, including an unset variable, is off."""
    source = os.environ if environ is None else environ
    value = source.get(ENFORCE_FROM_ENV, "").strip()
    if not value:
        return None
    if value.lower() in _TRUTHY:
        return EVERY_ROW_STAMP
    if _CUTOVER_RE.match(value):
        return value
    return None


def declared_values(line: str) -> dict[str, list[str]]:
    """The values of the keys this rule reads, from the cells of ``line`` that declare them.

    A pipe cell declares ``key=value`` words from its start (a cell may share its pipe
    field, as ``outcome=done delegated=1 runs=x`` does); reading stops at the first bare
    word, so a key in free text is never read. ``running_line`` is a line of words, so it
    takes the rest of its cell. The stamp and the type cells are skipped."""
    found: dict[str, list[str]] = {key: [] for key in DECLARED_KEYS}
    for cell in line.rstrip("\n").split("|")[2:]:
        text = cell.strip()
        while text:
            match = _KEY_VALUE_RE.match(text)
            if match is None:
                break
            key, value = match.group(1).lower(), match.group(2)
            if key == "running_line":
                found[key].append(text.split("=", 1)[1].strip())
                break
            if key in DECLARED_KEYS:
                found[key].append(value)
            text = text[match.end() :].lstrip()
    return found


def terminal_refusal(
    *,
    stamp: str,
    outcome: str | None,
    paths: list[str],
    lines: list[str],
    from_stamp: str | None,
) -> str | None:
    """Why a TERMINAL row breaks the running-path rule, or None.

    ``paths`` and ``lines`` are every ``running_path=`` and ``running_line=``
    value of the row. Judged only when the rule is on, the row is stamped at or
    after the cutover, and the outcome confirms the work."""
    if from_stamp is None or stamp < from_stamp:
        return None
    if (outcome or "").strip().lower() not in CONFIRMING_OUTCOMES:
        return None
    if not any(value.strip() for value in paths):
        return (
            f"TERMINAL outcome={(outcome or '').strip()} carries no running-path cell "
            f"(OMN-24535): add {SHAPE}"
        )
    if len(paths) > 1 or len([value for value in lines if value.strip()]) > 1:
        return (
            "TERMINAL carries more than one running_path= or running_line= cell "
            "(OMN-24535): name one"
        )
    path = next(value for value in paths if value.strip()).strip()
    lowered = path.lower()
    if lowered.startswith(NONE_CI_ONLY):
        reason = path[len(NONE_CI_ONLY) :]
        if (
            not reason.startswith(":")
            or reason[1:].lower() in PLACEHOLDERS
            or _REASON_RE.match(reason[1:]) is None
        ):
            return (
                "running_path=none-ci-only needs a reason: "
                "running_path=none-ci-only:<reason> (OMN-24535)"
            )
        return None
    if re.search(r"\s", path) or "<" in path or ">" in path or lowered in PLACEHOLDERS:
        return f"running_path={path!r} is not an entry point (OMN-24535): add {SHAPE}"
    line = next((value for value in lines if value.strip()), "").strip()
    if (
        not line
        or line.lower() in PLACEHOLDERS
        or "<" in line
        or ">" in line
        or line.lower() == lowered
    ):
        return (
            "running_path names the entry point but running_line carries no line from "
            "its latest run (OMN-24535): add running_line=<one line from its latest run "
            "showing the changed behaviour>"
        )
    return None
