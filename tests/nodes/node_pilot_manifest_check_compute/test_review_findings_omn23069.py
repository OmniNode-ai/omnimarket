# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings on omnimarket#5299 (OMN-23069) for the pilot manifest check.

Finding 1: a manifest with a naive and an aware timestamp must not crash the
handler; the input boundary refuses naive timestamps so every accepted
combination compares. Finding 2: the canonical bar hash must not depend on the
order of constraints that share a metric.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import permutations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pilot_manifest_check_compute.handlers.handler_pilot_manifest_check import (
    HandlerPilotManifestCheck,
    compute_bar_hash,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_bar_comparator import (
    EnumPilotBarComparator,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_check_finding import (
    EnumPilotCheckFinding,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_status import (
    EnumPilotStatus,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_bar_criterion import (
    ModelPilotBarCriterion,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest import (
    ModelPilotManifest,
)
from tests.nodes.node_pilot_manifest_check_compute.support import (
    FROZEN_AT,
    STARTED_AT,
    frozen_manifest,
    request,
)

pytestmark = pytest.mark.unit

_GE = EnumPilotBarComparator.GE
_LE = EnumPilotBarComparator.LE


def _manifest_json(frozen_at: str, started_at: str) -> dict[str, object]:
    return {
        "pilot": "memory-cognee-stage1",
        "bar": [{"metric": "overall_accuracy", "comparator": "ge", "threshold": 0.8}],
        "frozen_at": frozen_at,
        "started_at": started_at,
    }


def test_finding1_naive_frozen_with_aware_started_is_refused_at_the_boundary() -> None:
    with pytest.raises(ValidationError):
        ModelPilotManifest.model_validate(
            _manifest_json("2026-11-07T10:00:00", "2026-11-07T11:00:00Z")
        )


def test_finding1_aware_frozen_with_naive_started_is_refused_at_the_boundary() -> None:
    with pytest.raises(ValidationError):
        ModelPilotManifest.model_validate(
            _manifest_json("2026-11-07T10:00:00Z", "2026-11-07T11:00:00")
        )


def test_finding1_both_naive_is_refused_at_the_boundary() -> None:
    with pytest.raises(ValidationError):
        ModelPilotManifest.model_validate(
            _manifest_json("2026-11-07T10:00:00", "2026-11-07T11:00:00")
        )


def test_finding1_every_accepted_combination_returns_a_verdict() -> None:
    aware_other_zone = datetime.fromisoformat("2026-11-07T12:00:00+02:00")
    for frozen_at, started_at in (
        (FROZEN_AT, STARTED_AT),
        (aware_other_zone, STARTED_AT),
        (None, STARTED_AT),
    ):
        result = HandlerPilotManifestCheck().handle(
            request(frozen_manifest(frozen_at=frozen_at, started_at=started_at))
        )
        assert result.status in (EnumPilotStatus.READY, EnumPilotStatus.REFUSED)
    ok = HandlerPilotManifestCheck().handle(
        request(frozen_manifest(frozen_at=aware_other_zone))
    )
    assert ok.status is EnumPilotStatus.READY
    late = HandlerPilotManifestCheck().handle(
        request(frozen_manifest(frozen_at=datetime(2026, 11, 7, 12, 30, tzinfo=UTC)))
    )
    assert EnumPilotCheckFinding.NOT_FROZEN_BEFORE_START in late.findings


def test_finding2_same_metric_constraint_order_does_not_change_the_hash() -> None:
    low = ModelPilotBarCriterion(metric="accuracy", comparator=_GE, threshold=0.8)
    high = ModelPilotBarCriterion(metric="accuracy", comparator=_LE, threshold=0.95)
    assert compute_bar_hash((low, high)) == compute_bar_hash((high, low))


def test_finding2_every_permutation_of_mixed_constraints_hashes_alike() -> None:
    bar = (
        ModelPilotBarCriterion(metric="accuracy", comparator=_GE, threshold=0.8),
        ModelPilotBarCriterion(metric="accuracy", comparator=_LE, threshold=0.95),
        ModelPilotBarCriterion(metric="accuracy", comparator=_GE, threshold=0.5),
        ModelPilotBarCriterion(metric="recall", comparator=_GE, threshold=1.0),
    )
    hashes = {compute_bar_hash(p) for p in permutations(bar)}
    assert len(hashes) == 1


def test_finding2_reordered_same_metric_bar_is_not_refused_as_hash_mismatch() -> None:
    low = ModelPilotBarCriterion(metric="accuracy", comparator=_GE, threshold=0.8)
    high = ModelPilotBarCriterion(metric="accuracy", comparator=_LE, threshold=0.95)
    manifest = frozen_manifest(bar=(low, high), bar_hash=compute_bar_hash((low, high)))
    reordered = manifest.model_copy(update={"bar": (high, low)})
    result = HandlerPilotManifestCheck().handle(request(reordered))
    assert result.findings == ()
    assert result.status is EnumPilotStatus.READY


def test_finding2_distinct_bars_still_hash_differently() -> None:
    a = ModelPilotBarCriterion(metric="accuracy", comparator=_GE, threshold=0.8)
    b = ModelPilotBarCriterion(metric="accuracy", comparator=_GE, threshold=0.9)
    assert compute_bar_hash((a,)) != compute_bar_hash((b,))
