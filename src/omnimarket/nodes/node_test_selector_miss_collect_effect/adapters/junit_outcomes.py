# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One shard's junit XML read into per-test-file outcomes.

pytest's default junit family writes no ``file`` attribute, so a test file is read from the dotted ``classname``:
the module is the first segment named ``test_*`` (or ``conftest``), the segments above it are directories. A
classname with no such segment is not guessed at: the shard is reported in error.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from omnimarket.models.test_selector_reading import EnumFileOutcome

_PRECEDENCE: tuple[EnumFileOutcome, ...] = (
    EnumFileOutcome.ERROR,
    EnumFileOutcome.FAILED,
    EnumFileOutcome.PASSED,
    EnumFileOutcome.SKIPPED,
)
_COLLECTION_MESSAGE = "collection failure"


class JunitUnreadableError(ValueError):
    """The XML is not a junit report, or names a test the reader cannot place in a file."""


def file_of_dotted(dotted: str) -> str | None:
    """``tests.nodes.node_x.test_y.TestZ`` is ``tests/nodes/node_x/test_y.py``; None when no module segment exists."""
    parts = dotted.split(".")
    for index, part in enumerate(parts):
        if part.startswith("test_") or part == "conftest":
            return "/".join(parts[: index + 1]) + ".py"
    return None


def _merge(
    outcomes: dict[str, EnumFileOutcome], test_file: str, outcome: EnumFileOutcome
) -> None:
    known = outcomes.get(test_file)
    if known is None or _PRECEDENCE.index(outcome) < _PRECEDENCE.index(known):
        outcomes[test_file] = outcome


def read_junit(xml_text: bytes) -> tuple[dict[str, EnumFileOutcome], bool]:
    """(test file -> outcome, whether the shard reports a collection error).

    Raises :class:`JunitUnreadableError` for a document that is not a junit report or holds a test it cannot place.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise JunitUnreadableError(f"not XML: {exc}") from exc
    if root.tag not in {"testsuites", "testsuite"}:
        raise JunitUnreadableError(f"not a junit report: root element {root.tag!r}")
    outcomes: dict[str, EnumFileOutcome] = {}
    collection_error = False
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        error = case.find("error")
        is_collection = error is not None and (
            not classname or (error.get("message") or "") == _COLLECTION_MESSAGE
        )
        # A collection error carries the module in ``name`` and no classname.
        dotted = (
            (case.get("name") or "") if (is_collection and not classname) else classname
        )
        test_file = file_of_dotted(dotted)
        if test_file is None:
            raise JunitUnreadableError(f"cannot place test {dotted!r} in a file")
        if is_collection:
            collection_error = True
        if error is not None:
            outcome = EnumFileOutcome.ERROR
        elif case.find("failure") is not None:
            outcome = EnumFileOutcome.FAILED
        elif case.find("skipped") is not None:
            outcome = EnumFileOutcome.SKIPPED
        else:
            outcome = EnumFileOutcome.PASSED
        _merge(outcomes, test_file, outcome)
    return outcomes, collection_error


__all__: list[str] = ["JunitUnreadableError", "file_of_dotted", "read_junit"]
