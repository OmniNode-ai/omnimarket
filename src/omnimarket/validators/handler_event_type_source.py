# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A handler may not hand-type the event type it matches on (OMN-18013).

THE DEFECT CLASS, WITH A LIVE INSTANCE
--------------------------------------
``HandlerRuntimeCloseoutOrchestrator`` routed on::

    if event_type.endswith("closeout-preflight-completed.v1"):
    elif event_type.endswith("redeploy-completed.v1"):

Those suffixes only ever match the FULL TOPIC STRING. The auto-wiring consume boundary
stamps the ALIAS ``<producer>.<event-name>``
(``topic_constants.derive_event_type_alias_for_topic``), which carries no ``.v1``, so
EVERY real message fell through to the else-branch and the orchestrator restarted the
closeout instead of advancing it. Four green golden-chain tests never caught it, because
they hand-typed the topic form as well.

THE RULE
--------
The event type a handler matches is DERIVED from a topic the contract declares -- never
typed into the handler. The sanctioned shape resolves the topic from the node's own
contract and derives every spelling the runtime can stamp::

    _SUBSCRIBE = contract_subscribe_topics(_CONTRACT)
    ...
    topic = the one _SUBSCRIBE entry ending in <selector>      # fails closed otherwise
    MATCH = frozenset({topic, derive_event_type_alias_for_topic(topic)})
    ...
    if event_type in MATCH:

WHAT THIS GATE REFUSES
----------------------
Any ``==`` / ``!=`` / ``in`` comparison, or ``.endswith(...)`` / ``.startswith(...)``
call, whose subject is an ``event_type``-shaped name or attribute and whose operand is a
VERSION-SUFFIXED STRING LITERAL (``...vN``) -- i.e. a topic or topic-tail typed by hand.

It reads the AST, so a docstring or comment that merely quotes the forbidden shape (this
module's own does) is not a match. There is no baseline and no allowlist.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SCAN_ROOT = Path("src/omnimarket")

# A hand-typed topic or topic tail: anything ending in a version segment.
_VERSIONED = re.compile(r"\.v\d+$")
# The name of the thing being compared: event_type, evt_type, _event_type, event_topic...
_SUBJECT = re.compile(r"(^|_)(event_type|event_topic|message_type)$")

# A scan finding far fewer files than the tree has is a broken scan, not a clean tree.
DEFAULT_MIN_EXPECTED_FILES = 400


@dataclass(frozen=True, slots=True)  # internal-dataclass-ok: validator-internal finding
class HandTypedMatch:
    """One handler site matching an event type against a hand-typed literal."""

    path: str
    line: int
    literal: str
    shape: str


def _subject_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_subject(node: ast.expr) -> bool:
    name = _subject_name(node)
    return name is not None and bool(_SUBJECT.search(name))


def _versioned_literals(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value] if _VERSIONED.search(node.value) else []
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [lit for element in node.elts for lit in _versioned_literals(element)]
    return []


def scan_source(text: str, path: str) -> list[HandTypedMatch]:
    findings: list[HandTypedMatch] = []
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"endswith", "startswith"}
                and _is_subject(func.value)
            ):
                for argument in node.args:
                    for literal in _versioned_literals(argument):
                        findings.append(
                            HandTypedMatch(path, node.lineno, literal, f"{func.attr}()")
                        )
        elif isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if not any(_is_subject(o) for o in operands):
                continue
            for operand in operands:
                for literal in _versioned_literals(operand):
                    findings.append(
                        HandTypedMatch(path, node.lineno, literal, "comparison")
                    )
    return findings


def scan(root: Path) -> tuple[list[HandTypedMatch], int]:
    findings: list[HandTypedMatch] = []
    files = 0
    here = Path(__file__).resolve()
    for path in sorted(root.rglob("*.py")):
        if path.resolve() == here:
            continue  # this module quotes the forbidden shape in its own docstring
        files += 1
        try:
            findings.extend(scan_source(path.read_text(errors="replace"), str(path)))
        except (OSError, SyntaxError):
            continue
    return findings, files


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    root = Path(args[0]) if args else DEFAULT_SCAN_ROOT
    minimum = int(args[1]) if len(args) > 1 else DEFAULT_MIN_EXPECTED_FILES

    findings, files = scan(root)
    if files < minimum:
        sys.stderr.write(
            f"[handler-event-type-source] FAIL (vacuity guard): only {files} python "
            f"file(s) under {root} (expected >= {minimum}). A gate over a collapsed set "
            f"proves nothing.\n"
        )
        return 1

    if findings:
        sys.stderr.write(
            f"[handler-event-type-source] FAIL: {len(findings)} site(s) match an event "
            f"type against a hand-typed, version-suffixed literal. The bus stamps the "
            f"ALIAS <producer>.<event-name>, which has no version segment, so these "
            f"branches are dead on every real message (OMN-18013):\n"
        )
        for f in findings:
            sys.stderr.write(f"  - {f.path}:{f.line}  {f.shape}  {f.literal!r}\n")
        sys.stderr.write(
            "\n  Fix: resolve the topic from the node's OWN contract "
            "(contract_subscribe_topics) and match against "
            "{topic, derive_event_type_alias_for_topic(topic)} -- see "
            "handler_runtime_closeout_orchestrator.py::_match_keys.\n"
        )
        return 1

    sys.stderr.write(
        f"[handler-event-type-source] OK: {files} file(s) scanned, 0 hand-typed event "
        f"type matches.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
