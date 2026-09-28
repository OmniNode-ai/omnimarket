# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPytestFailureDigest — junit XML to a typed, bounded failure digest
(OMN-19360, task T4 of the delegated test loop first slice).

Pure definition-B compute: ``handle(request: ModelPytestRunReport) ->
ModelPytestFailureDigest``. No I/O, no clock, no envelope type.

A port of the archived bridge's pytest result parser, retargeted from the
JSON-report plugin to pytest's built-in ``--junitxml`` so the test image needs
no extra plugin. The phase comes from how pytest records it:

* ``<failure>`` on a testcase is a call-phase failure (the assertion ran);
* ``<error message="collection failure">`` on a testcase with an empty
  classname is a collection failure (the module never imported);
* any other ``<error>`` ("failed on setup with ...", "failed on teardown
  with ...") is a fixture error;
* ``<skipped>`` is not a pass. A file whose every test was skipped, or which
  ran no test at all (pytest exit code 5), is ``no_tests``.

The exit code has to agree with the file. An empty, unparsable or missing
file, a missing exit code, or an exit code that contradicts the file (exit 0
with a failure in it; a failure exit with every test passing) is
``infra_error``. An infrastructure fault is never a pass.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET

from omnimarket.nodes.node_pytest_failure_digest_compute.models.model_pytest_failure_digest import (
    MAX_FRAMES_CHARS,
    MAX_MESSAGE_CHARS,
    EnumPytestRunOutcome,
    ModelPytestFailureDigest,
    ModelPytestRunReport,
)

_EXIT_OK = 0
_EXIT_NO_TESTS = 5
_FRAME_LINE = re.compile(
    r"^(?P<file>[^\s:][^:\n]*\.py):(?P<line>\d+):(?: (?P<rest>.*))?$", re.M
)
_EXCEPTION_NAME = re.compile(r"^[A-Za-z_][\w.]*$")
_E_LINE_EXCEPTION = re.compile(
    r"^E\s+(?P<name>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt))\b", re.M
)
_MESSAGE_EXCEPTION = re.compile(
    r"^(?P<name>[A-Za-z_][\w.]*(?:Error|Exception|Warning)):"
)
_COLLECTION_MESSAGE = "collection failure"


def _count(root: ET.Element, attribute: str) -> int:
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = 0
    for suite in suites:
        try:
            total += int(suite.get(attribute, "0"))
        except ValueError:
            continue
    return total


def _crash_details(problem: ET.Element) -> tuple[str, str, str, str]:
    """(exception type, message, frames, top frame) for one failure or error."""
    body = (problem.text or "").strip()
    raw_message = (problem.get("message") or "").strip()

    top_frame = ""
    exception_type = ""
    frames = list(_FRAME_LINE.finditer(body))
    if frames:
        last = frames[-1]
        top_frame = f"{last.group('file')}:{last.group('line')}"
        rest = (last.group("rest") or "").strip()
        if _EXCEPTION_NAME.fullmatch(rest) and not rest.startswith("in"):
            exception_type = rest
    if not exception_type:
        match = _E_LINE_EXCEPTION.search(body)
        if match:
            exception_type = match.group("name")
    if not exception_type:
        match = _MESSAGE_EXCEPTION.match(raw_message.strip('"'))
        if match:
            exception_type = match.group("name")
    if not exception_type and raw_message.startswith("assert"):
        exception_type = "AssertionError"

    message = raw_message
    if message == _COLLECTION_MESSAGE or not message:
        e_lines = [
            line[1:].strip() for line in body.splitlines() if line.startswith("E ")
        ]
        message = e_lines[0] if e_lines else message
    return (
        exception_type,
        message[:MAX_MESSAGE_CHARS],
        body[-MAX_FRAMES_CHARS:].strip(),
        top_frame,
    )


def _fingerprint(
    outcome: EnumPytestRunOutcome, node: str, exception: str, frame: str
) -> str:
    material = "\0".join((outcome.value, node, exception, frame))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def digest_pytest_run(request: ModelPytestRunReport) -> ModelPytestFailureDigest:
    """Classify one run and describe its first problem, bounded."""
    infra = ModelPytestFailureDigest(outcome=EnumPytestRunOutcome.INFRA_ERROR)
    if not request.junit_xml.strip() or request.exit_code is None:
        return infra
    try:
        root = ET.fromstring(request.junit_xml)
    except ET.ParseError:
        return infra
    if root.tag not in {"testsuites", "testsuite"}:
        return infra

    collection: list[tuple[ET.Element, ET.Element]] = []
    setup: list[tuple[ET.Element, ET.Element]] = []
    call: list[tuple[ET.Element, ET.Element]] = []
    passed = 0
    for case in root.iter("testcase"):
        failure = case.find("failure")
        error = case.find("error")
        if error is not None:
            message = (error.get("message") or "").strip()
            if message == _COLLECTION_MESSAGE or not case.get("classname"):
                collection.append((case, error))
            else:
                setup.append((case, error))
        elif failure is not None:
            call.append((case, failure))
        elif case.find("skipped") is None:
            passed += 1

    counts = {
        "tests": _count(root, "tests"),
        "failures": _count(root, "failures"),
        "errors": _count(root, "errors"),
        "skipped": _count(root, "skipped"),
    }
    exit_code = request.exit_code
    for outcome, problems in (
        (EnumPytestRunOutcome.FAILED_COLLECTION, collection),
        (EnumPytestRunOutcome.ERROR_SETUP, setup),
        (EnumPytestRunOutcome.FAILED_CALL, call),
    ):
        if not problems:
            continue
        if exit_code == _EXIT_OK:
            return infra  # a problem in the file and a clean exit disagree
        case, problem = problems[0]
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        node = f"{classname}::{name}" if classname else name
        exception_type, message, frames, top_frame = _crash_details(problem)
        return ModelPytestFailureDigest(
            outcome=outcome,
            failing_node_id=node,
            exception_type=exception_type,
            message=message,
            frames=frames,
            top_frame=top_frame,
            fingerprint=_fingerprint(outcome, node, exception_type, top_frame),
            **counts,
        )

    if passed == 0:
        if exit_code not in (_EXIT_OK, _EXIT_NO_TESTS):
            return infra
        return ModelPytestFailureDigest(
            outcome=EnumPytestRunOutcome.NO_TESTS,
            fingerprint=_fingerprint(EnumPytestRunOutcome.NO_TESTS, "", "", ""),
            **counts,
        )
    if exit_code != _EXIT_OK:
        return infra  # every test passed and the process still failed
    return ModelPytestFailureDigest(outcome=EnumPytestRunOutcome.PASSED, **counts)


class HandlerPytestFailureDigest:
    """COMPUTE handler: one run's junit XML in, one bounded digest out."""

    def handle(self, request: ModelPytestRunReport) -> ModelPytestFailureDigest:
        return digest_pytest_run(request)


__all__ = ["HandlerPytestFailureDigest", "digest_pytest_run"]
