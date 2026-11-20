# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Validate the closer's scope bundle before it is handed to the workflow script.

A sweep once died because ``bundle.held`` was not an array, with an empty output file and the
error text cut off, so nobody could read why. The workflow script refuses a bundle it cannot
read, but only after an agent has relayed (and possibly mangled) the text, and with a throw that a
long message can bury.

``check-bundle`` runs directly on the bundle command's own output, applies the same schema the
script applies, and on success prints the bundle unchanged (exit 0). On failure it writes a
readable error file under the state directory, prints
``BUNDLE_FAILED: <first problem>; error_file=<path>`` and exits 2.

The error file is ``$ONEX_STATE_DIR/closeout-sweep/errors/<run>.bundle-error.txt``;
``ONEX_STATE_DIR`` defaults to ``~/.local/state/onex``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

BUNDLE_LIST_KEYS = ("candidates", "pr_state", "text_state", "held", "waiting")
BUNDLE_TOTALS = ("candidates", "eligible", "selected", "deferred", "held", "waiting")
RUN_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
HEAD_CHARS = 500


def state_dir() -> Path:
    configured = os.environ.get("ONEX_STATE_DIR")
    return Path(configured) if configured else Path.home() / ".local/state/onex"


def error_path(run: str) -> Path:
    if not RUN_KEY.fullmatch(run):
        raise ValueError(f"closeout-scope: invalid run key {run!r}")
    return state_dir() / "closeout-sweep/errors" / f"{run}.bundle-error.txt"


def _kind(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def problems_of(raw: str) -> list[str]:
    """Every way the bundle breaks the script's schema, one readable line each."""
    try:
        bundle = json.loads(raw)
    except json.JSONDecodeError as error:
        return [f"the bundle is not JSON ({error.msg} at character {error.pos})"]
    if not isinstance(bundle, dict):
        return [f"the bundle is {_kind(bundle)}, not an object"]
    found: list[str] = []
    for key in BUNDLE_LIST_KEYS:
        if not isinstance(bundle.get(key), list):
            found.append(f"bundle.{key} is {_kind(bundle.get(key))}, not an array")
    totals = bundle.get("totals")
    if not isinstance(totals, dict):
        found.append(f"bundle.totals is {_kind(totals)}, not an object")
        totals = {}
    for key in BUNDLE_TOTALS:
        if not _is_count(totals.get(key)):
            found.append(
                f"bundle.totals.{key} is {_kind(totals.get(key))}, not a count"
            )
    for key in ("held_count", "waiting_count"):
        if not _is_count(bundle.get(key)):
            found.append(f"bundle.{key} is {_kind(bundle.get(key))}, not a count")
    held = bundle.get("held")
    if isinstance(held, list) and not all(isinstance(x, str) and x for x in held):
        found.append("bundle.held is not a list of ids")
    waiting = bundle.get("waiting")
    if isinstance(waiting, list) and not all(
        isinstance(w, dict)
        and isinstance(w.get("id"), str)
        and w["id"]
        and isinstance(w.get("reason"), str)
        for w in waiting
    ):
        found.append("bundle.waiting is not a list of {id, reason}")
    candidates = bundle.get("candidates")
    if (
        isinstance(candidates, list)
        and _is_count(totals.get("selected"))
        and len(candidates) != totals["selected"]
    ):
        found.append(
            f"bundle.candidates carries {len(candidates)} rows but "
            f"totals.selected is {totals['selected']}"
        )
    return found


def write_error(
    path: Path,
    run: str,
    problems: list[str],
    raw: str,
    stderr_text: str,
    exit_code: int,
) -> None:
    lines = [
        f"dod-closeout-sweep scope bundle refused (run {run})",
        "",
        *(f"- {problem}" for problem in problems),
        "",
        f"bundle command exit code: {exit_code}",
        f"bundle length: {len(raw)} characters",
        f"bundle head: {raw[:HEAD_CHARS]!r}",
    ]
    if stderr_text.strip():
        lines += [
            "",
            "bundle command stderr (first 2000 characters):",
            stderr_text[:2000],
        ]
    lines += [
        "",
        "Nothing downstream ran on this bundle: a bundle that cannot be read is never a zero.",
        "Re-run the bundle command by hand to see its own output; the next fire starts clean.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".txt.tmp")
    temporary.write_text("\n".join(lines) + "\n")
    temporary.replace(path)


def check(
    bundle_file: Path, stderr_file: Path | None, exit_code: int, run: str
) -> tuple[int, str]:
    """The exit code and the one line to print: the bundle itself, or the refusal."""
    raw = bundle_file.read_text() if bundle_file.is_file() else ""
    stderr_text = (
        stderr_file.read_text() if stderr_file and stderr_file.is_file() else ""
    )
    problems = problems_of(raw) if exit_code == 0 and raw.strip() else []
    if exit_code != 0:
        problems = [f"the bundle command exited {exit_code}"]
    elif not raw.strip():
        problems = ["the bundle command printed nothing"]
    if not problems:
        return 0, raw.strip()
    path = error_path(run)
    write_error(path, run, problems, raw, stderr_text, exit_code)
    return 2, f"BUNDLE_FAILED: {problems[0]}; error_file={path}"
