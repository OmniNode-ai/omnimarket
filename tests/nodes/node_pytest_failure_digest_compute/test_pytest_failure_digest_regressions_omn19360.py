# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19360 regressions for report shape, invalid runs and timing output."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from omnimarket.nodes.node_pytest_failure_digest_compute.handlers.handler_pytest_failure_digest import (
    HandlerPytestFailureDigest,
)
from omnimarket.nodes.node_pytest_failure_digest_compute.models.model_pytest_failure_digest import (
    EnumPytestRunOutcome,
    ModelPytestFailureDigest,
    ModelPytestRunReport,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("fixture", "exit_code", "outcome", "counts"),
    [
        ("container_passed", 0, EnumPytestRunOutcome.PASSED, (1, 0, 0, 0)),
        ("call", 1, EnumPytestRunOutcome.FAILED_CALL, (1, 1, 0, 0)),
        ("setup", 1, EnumPytestRunOutcome.ERROR_SETUP, (1, 0, 1, 0)),
        ("collect", 2, EnumPytestRunOutcome.FAILED_COLLECTION, (1, 0, 1, 0)),
        (
            "container_collection",
            4,
            EnumPytestRunOutcome.FAILED_COLLECTION,
            (1, 0, 1, 0),
        ),
        ("empty", 5, EnumPytestRunOutcome.NO_TESTS, (0, 0, 0, 0)),
        ("skip", 0, EnumPytestRunOutcome.NO_TESTS, (1, 0, 0, 1)),
    ],
)
def test_fixture_outcomes_and_counts_accept_a_single_testsuite_root(
    fixture: str,
    exit_code: int,
    outcome: EnumPytestRunOutcome,
    counts: tuple[int, int, int, int],
) -> None:
    root = ET.fromstring((FIXTURES / f"junit_{fixture}.xml").read_text())
    suite = root.find("testsuite")
    assert suite is not None
    report = ModelPytestRunReport(
        junit_xml=ET.tostring(suite, encoding="unicode"), exit_code=exit_code
    )

    digest = HandlerPytestFailureDigest().handle(report)

    assert digest.outcome is outcome
    assert (digest.tests, digest.failures, digest.errors, digest.skipped) == counts


@pytest.mark.parametrize("exit_code", [1, 5, 137, None])
@pytest.mark.parametrize(
    "junit", ["", " \n\t ", "<testsuites><testsuite", "not xml at all"]
)
def test_invalid_junit_cannot_be_rescued_by_a_nonzero_or_missing_exit_code(
    junit: str, exit_code: int | None
) -> None:
    digest = HandlerPytestFailureDigest().handle(
        ModelPytestRunReport(junit_xml=junit, exit_code=exit_code)
    )

    # In particular, exit 5 cannot turn an absent report into a no-tests run.
    assert digest == ModelPytestFailureDigest(outcome=EnumPytestRunOutcome.INFRA_ERROR)
    assert digest.outcome is not EnumPytestRunOutcome.PASSED


def test_timing_in_failure_message_and_traceback_does_not_change_fingerprint() -> None:
    root = ET.fromstring((FIXTURES / "junit_call.xml").read_text())
    failure = root.find(".//failure")
    assert failure is not None
    failure.set("message", f"{failure.get('message')}\nElapsed: 0.001s")
    failure.text = f"{failure.text}\nCaptured duration: 0.001s"
    first_xml = ET.tostring(root, encoding="unicode")
    second_xml = first_xml.replace("0.001s", "9.999s")
    handler = HandlerPytestFailureDigest()

    first = handler.handle(ModelPytestRunReport(junit_xml=first_xml, exit_code=1))
    second = handler.handle(ModelPytestRunReport(junit_xml=second_xml, exit_code=1))

    assert first.outcome is second.outcome is EnumPytestRunOutcome.FAILED_CALL
    assert first.message != second.message
    assert first.frames != second.frames
    assert (
        first.failing_node_id
        == second.failing_node_id
        == "tests.test_call::test_call_fails"
    )
    assert first.exception_type == second.exception_type == "AssertionError"
    assert first.top_frame == second.top_frame == "tests/test_call.py:6"
    assert first.fingerprint
    assert first.fingerprint == second.fingerprint
