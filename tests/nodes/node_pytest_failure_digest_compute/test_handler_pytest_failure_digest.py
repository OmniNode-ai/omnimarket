# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19360 — the pytest failure digest compute.

Fixtures are real pytest junit files: five produced by pytest 9.1 on tiny
tests written for each outcome, and two produced by the delegated test loop's
container on .101 during the T0 calibration (the hidden OMN-19193 AC2 test at
the fixed ref, and the same test at the pre-fix ref, where it fails at
collection). Host names and timestamps are normalised; nothing else is edited.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_pytest_failure_digest_compute.handlers.handler_pytest_failure_digest import (
    HandlerPytestFailureDigest,
    digest_pytest_run,
)
from omnimarket.nodes.node_pytest_failure_digest_compute.models.model_pytest_failure_digest import (
    MAX_FRAMES_CHARS,
    MAX_MESSAGE_CHARS,
    EnumPytestRunOutcome,
    ModelPytestRunReport,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"


def _junit(name: str) -> str:
    return (FIXTURES / f"junit_{name}.xml").read_text()


def _digest(junit: str, exit_code: int | None):  # type: ignore[no-untyped-def]
    return digest_pytest_run(ModelPytestRunReport(junit_xml=junit, exit_code=exit_code))


@pytest.mark.parametrize(
    ("fixture", "exit_code", "outcome"),
    [
        ("container_passed", 0, EnumPytestRunOutcome.PASSED),
        ("call", 1, EnumPytestRunOutcome.FAILED_CALL),
        ("setup", 1, EnumPytestRunOutcome.ERROR_SETUP),
        ("collect", 2, EnumPytestRunOutcome.FAILED_COLLECTION),
        ("container_collection", 4, EnumPytestRunOutcome.FAILED_COLLECTION),
        ("empty", 5, EnumPytestRunOutcome.NO_TESTS),
        ("skip", 0, EnumPytestRunOutcome.NO_TESTS),
    ],
)
def test_each_fixture_maps_to_its_outcome(
    fixture: str, exit_code: int, outcome: EnumPytestRunOutcome
) -> None:
    assert _digest(_junit(fixture), exit_code).outcome is outcome


@pytest.mark.parametrize(
    "junit", ["", "   \n", "<testsuites><testsuite", "not xml at all"]
)
def test_a_missing_empty_or_broken_junit_file_is_infra_error_never_passed(
    junit: str,
) -> None:
    digest = _digest(junit, 0)
    assert digest.outcome is EnumPytestRunOutcome.INFRA_ERROR
    assert digest.outcome is not EnumPytestRunOutcome.PASSED


@pytest.mark.parametrize(
    ("fixture", "exit_code"),
    [
        ("container_passed", None),  # it never reported an exit code
        ("container_passed", 1),  # the process failed though every test passed
        ("container_passed", 137),  # killed
        ("call", 0),  # exit 0 with a failure in the file: contradictory
    ],
)
def test_an_exit_code_that_contradicts_the_file_is_infra_error(
    fixture: str, exit_code: int | None
) -> None:
    assert (
        _digest(_junit(fixture), exit_code).outcome is EnumPytestRunOutcome.INFRA_ERROR
    )


def test_a_call_failure_names_its_node_exception_and_crash_frame() -> None:
    digest = _digest(_junit("call"), 1)
    assert digest.failing_node_id == "tests.test_call::test_call_fails"
    assert digest.exception_type == "AssertionError"
    assert digest.top_frame == "tests/test_call.py:6"
    assert digest.message.startswith("assert 2 == 3")
    assert "helper(1)" in digest.frames
    assert (digest.tests, digest.failures, digest.errors) == (1, 1, 0)
    assert re.fullmatch(r"[0-9a-f]{64}", digest.fingerprint)


def test_a_collection_failure_names_the_import_error() -> None:
    digest = _digest(_junit("container_collection"), 4)
    assert digest.exception_type == "ImportError"
    assert "SnapshotPublishFromRunningLoopError" in digest.message + digest.frames
    assert (
        digest.top_frame
        == "tests/unit/projection/test_local_evidence_no_broker_publish.py:59"
    )


def test_a_setup_error_names_the_fixture_exception() -> None:
    digest = _digest(_junit("setup"), 1)
    assert digest.exception_type == "RuntimeError"
    assert digest.top_frame == "tests/test_setup.py:6"


def test_a_pass_carries_no_fingerprint() -> None:
    digest = _digest(_junit("container_passed"), 0)
    assert digest.fingerprint == ""
    assert digest.failing_node_id == ""


def test_two_reports_differing_only_in_timing_have_the_same_fingerprint() -> None:
    first = _junit("call")
    second = re.sub(r'time="[0-9.]+"', 'time="9.999"', first)
    second = second.replace('hostname="fixture"', 'hostname="another-host"')
    second = second.replace("2026-09-23T00:00:00+00:00", "2027-01-01T12:34:56+00:00")
    assert second != first
    assert _digest(first, 1).fingerprint == _digest(second, 1).fingerprint


def test_a_different_failure_has_a_different_fingerprint() -> None:
    assert (
        _digest(_junit("call"), 1).fingerprint
        != _digest(_junit("setup"), 1).fingerprint
    )


def test_message_and_frames_are_capped() -> None:
    long_message = "x" * 5000
    long_body = "\n".join(f"E   line {i} " + "y" * 80 for i in range(200))
    junit = (
        '<testsuites><testsuite errors="0" failures="1" skipped="0" tests="1">'
        f'<testcase classname="tests.test_big" name="test_big"><failure message="{long_message}">'
        f"{long_body}\n\ntests/test_big.py:3: AssertionError</failure></testcase>"
        "</testsuite></testsuites>"
    )
    digest = _digest(junit, 1)
    assert digest.outcome is EnumPytestRunOutcome.FAILED_CALL
    assert len(digest.message) <= MAX_MESSAGE_CHARS
    assert len(digest.frames) <= MAX_FRAMES_CHARS
    assert digest.frames.endswith("tests/test_big.py:3: AssertionError")


async def test_the_handler_is_the_same_pure_function() -> None:
    report = ModelPytestRunReport(junit_xml=_junit("call"), exit_code=1)
    assert HandlerPytestFailureDigest().handle(report) == digest_pytest_run(report)


def test_the_handler_imports_no_envelope_type() -> None:
    node_dir = (
        Path(__file__).parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_pytest_failure_digest_compute"
    )
    sources = [p.read_text() for p in node_dir.rglob("*.py") if "tests" not in p.parts]
    assert sources
    assert not [s for s in sources if "ModelEventEnvelope" in s]
