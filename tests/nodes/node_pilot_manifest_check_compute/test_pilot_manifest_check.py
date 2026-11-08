# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pilot manifest check (OMN-22979, rebuild plan 3.5 row 16 and 7.2).

AC1 selector: bar_and_hash. AC2 selector: bar_failure_rejects.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_pilot_manifest_check_compute.handlers.handler_pilot_manifest_check import (
    HandlerPilotManifestCheck,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_check_finding import (
    EnumPilotCheckFinding,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_status import (
    EnumPilotStatus,
)
from tests.nodes.node_pilot_manifest_check_compute.support import (
    STARTED_AT,
    frozen_manifest,
    request,
)

pytestmark = pytest.mark.unit


def test_bar_and_hash_both_present_passes() -> None:
    result = HandlerPilotManifestCheck().handle(request(frozen_manifest()))
    assert result.check_passed is True
    assert result.findings == ()
    assert result.status is EnumPilotStatus.READY


def test_bar_and_hash_missing_bar_fails() -> None:
    result = HandlerPilotManifestCheck().handle(
        request(frozen_manifest(bar=(), bar_hash=None))
    )
    assert result.check_passed is False
    assert EnumPilotCheckFinding.BAR_MISSING in result.findings
    assert result.status is EnumPilotStatus.REFUSED


def test_bar_and_hash_missing_hash_fails() -> None:
    result = HandlerPilotManifestCheck().handle(request(frozen_manifest(bar_hash=None)))
    assert result.check_passed is False
    assert result.findings == (EnumPilotCheckFinding.HASH_MISSING,)
    assert result.status is EnumPilotStatus.REFUSED


def test_bar_and_hash_altered_bar_fails_hash_mismatch() -> None:
    manifest = frozen_manifest()
    altered = manifest.model_copy(
        update={
            "bar": (
                manifest.bar[0].model_copy(update={"threshold": 0.1}),
                manifest.bar[1],
            )
        }
    )
    result = HandlerPilotManifestCheck().handle(request(altered))
    assert result.check_passed is False
    assert result.findings == (EnumPilotCheckFinding.HASH_MISMATCH,)


def test_bar_and_hash_frozen_after_start_fails() -> None:
    result = HandlerPilotManifestCheck().handle(
        request(frozen_manifest(frozen_at=STARTED_AT.replace(hour=12)))
    )
    assert result.check_passed is False
    assert result.findings == (EnumPilotCheckFinding.NOT_FROZEN_BEFORE_START,)


def test_bar_and_hash_unfrozen_time_fails() -> None:
    result = HandlerPilotManifestCheck().handle(
        request(frozen_manifest(frozen_at=None))
    )
    assert result.check_passed is False
    assert result.findings == (EnumPilotCheckFinding.NOT_FROZEN_BEFORE_START,)


def test_bar_failure_rejects_when_a_criterion_misses() -> None:
    result = HandlerPilotManifestCheck().handle(
        request(
            frozen_manifest(),
            results={"overall_accuracy": 0.7, "planted_recall": 1.0},
        )
    )
    assert result.status is EnumPilotStatus.REJECTED
    assert result.failed_metrics == ("overall_accuracy",)


def test_bar_failure_rejects_not_extended_when_extension_requested() -> None:
    result = HandlerPilotManifestCheck().handle(
        request(
            frozen_manifest(),
            results={"overall_accuracy": 0.7, "planted_recall": 1.0},
            extension_requested=True,
        )
    )
    assert result.status is EnumPilotStatus.REJECTED
    assert result.extension_refused is True


def test_bar_failure_rejects_an_unread_metric() -> None:
    result = HandlerPilotManifestCheck().handle(
        request(frozen_manifest(), results={"overall_accuracy": 0.9})
    )
    assert result.status is EnumPilotStatus.REJECTED
    assert result.failed_metrics == ("planted_recall",)


def test_bar_failure_rejects_leaves_a_met_bar_accepted() -> None:
    result = HandlerPilotManifestCheck().handle(
        request(
            frozen_manifest(),
            results={"overall_accuracy": 0.8, "planted_recall": 1.0},
            extension_requested=True,
        )
    )
    assert result.status is EnumPilotStatus.ACCEPTED
    assert result.failed_metrics == ()
    assert result.extension_refused is False


def test_bar_failure_rejects_never_scores_a_pilot_the_check_refused() -> None:
    result = HandlerPilotManifestCheck().handle(
        request(
            frozen_manifest(bar_hash=None),
            results={"overall_accuracy": 1.0, "planted_recall": 1.0},
        )
    )
    assert result.status is EnumPilotStatus.REFUSED
    assert result.failed_metrics == ()
