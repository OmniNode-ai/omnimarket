# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_worker_memory_admission_compute [OMN-14977].

Verifies contract/metadata YAML validity, handler + model imports, the
headroom formula, the fail-closed staleness gate, and the typed-refusal
invariants encoded in ModelMemoryAdmissionReceipt. This is the acceptance
suite for plan §2 D3
(docs/plans/2026-07-23-distributed-validation-context-aware-runtime-plan.md).

Directly-invoked only (no Kafka producer/consumer yet — see contract.yaml
header comment): this node declares no ``event_bus`` topics, so it
contributes no edges to the contract-topic-graph gate. It is reachable via
``handler.handle()`` (e.g. the not-yet-built push-validation worker's inline
admission check) or the local ``onex node`` compute-execution path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_worker_memory_admission_compute.handlers.handler_worker_memory_admission_compute import (
    HandlerWorkerMemoryAdmissionCompute,
)
from omnimarket.nodes.node_worker_memory_admission_compute.models import (
    EnumMidRunCollapsePolicy,
    ModelHostMemoryAdvertisement,
    ModelMemoryAdmissionRequest,
)

NODE_NAME = "node_worker_memory_admission_compute"

# 128 GiB total, matching the plan's own 2026-07-22 measured example order of
# magnitude (104 GB wired observed on ds4-server).
_TOTAL = 128 * 1024**3
_WIRED = 104 * 1024**3
_RESERVATION = 8 * 1024**3
_CADENCE = 30


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / NODE_NAME
    )


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        import yaml

        data = yaml.safe_load(contract_path.read_text())
        assert isinstance(data, dict)
        assert data["name"] == NODE_NAME
        assert data["node_type"] == "COMPUTE_GENERIC"

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        import yaml

        data = yaml.safe_load(contract_path.read_text())
        handler = data.get("handler", {})
        assert "module" in handler
        assert "class" in handler
        routed = data["handler_routing"]["handlers"][0]["handler"]
        assert routed["name"] == handler["class"]

    def test_contract_declares_no_bus_topics(self, contract_path: Path) -> None:
        """No producer for the admission COMPUTE's inputs exists yet (ticket
        residual) — the contract deliberately declares no event_bus block so
        it contributes no orphaned edges to the contract-topic-graph gate."""
        import yaml

        data = yaml.safe_load(contract_path.read_text())
        assert "event_bus" not in data
        assert "terminal_event" not in data

    def test_contract_declares_runtime_dispatch_seam(self, contract_path: Path) -> None:
        import yaml

        data = yaml.safe_load(contract_path.read_text())
        dispatch = data["runtime_dispatch"]
        assert dispatch["command_topic"]
        assert dispatch["terminal_events"]["success"]
        assert dispatch["terminal_events"]["failure"]

    def test_descriptor_declares_pure_compute(self, contract_path: Path) -> None:
        import yaml

        data = yaml.safe_load(contract_path.read_text())
        descriptor = data["descriptor"]
        assert descriptor["node_archetype"] == "compute"
        assert descriptor["purity"] == "pure"


class TestMetadataYaml:
    def test_metadata_loads(self, metadata_path: Path) -> None:
        import yaml

        data = yaml.safe_load(metadata_path.read_text())
        assert data["name"] == NODE_NAME
        assert "version" in data
        assert "entry_points" in data
        assert (
            data["entry_points"]["onex.nodes"][NODE_NAME]
            == "omnimarket.nodes.node_worker_memory_admission_compute"
        )


class TestImports:
    def test_handler_imports(self) -> None:
        assert HandlerWorkerMemoryAdmissionCompute is not None

    def test_models_import(self) -> None:
        from omnimarket.nodes.node_worker_memory_admission_compute.models import (
            EnumMemoryAdmissionOutcome,
            EnumMemoryAdmissionRefusalReason,
            ModelMemoryAdmissionReceipt,
        )

        assert EnumMemoryAdmissionOutcome.ADMITTED.value == "admitted"
        assert (
            EnumMemoryAdmissionRefusalReason.STALE_ADVERTISEMENT.value
            == "stale_advertisement"
        )
        assert ModelHostMemoryAdvertisement is not None
        assert ModelMemoryAdmissionRequest is not None
        assert ModelMemoryAdmissionReceipt is not None


def _advertisement(
    *,
    total_bytes: int = _TOTAL,
    wired_bytes: int = _WIRED,
    inference_reservation_bytes: int = _RESERVATION,
    cadence_seconds: int = _CADENCE,
    advertised_at: str = "2026-07-24T00:00:00.000000Z",
    collapse_policy: EnumMidRunCollapsePolicy = EnumMidRunCollapsePolicy.ABORT,
) -> ModelHostMemoryAdvertisement:
    return ModelHostMemoryAdvertisement(
        host_identity="omninode-pc",
        total_bytes=total_bytes,
        wired_bytes=wired_bytes,
        inference_reservation_bytes=inference_reservation_bytes,
        cadence_seconds=cadence_seconds,
        collapse_policy=collapse_policy,
        advertised_at=advertised_at,
    )


class TestHeadroomFormula:
    def test_usable_bytes_is_total_minus_wired_minus_reservation(self) -> None:
        advertisement = _advertisement()
        assert advertisement.usable_bytes == _TOTAL - _WIRED - _RESERVATION

    def test_usable_bytes_floors_at_zero_never_negative(self) -> None:
        advertisement = _advertisement(
            total_bytes=10, wired_bytes=8, inference_reservation_bytes=8
        )
        assert advertisement.usable_bytes == 0


class TestAdmissionChain:
    def _handler(self) -> HandlerWorkerMemoryAdmissionCompute:
        return HandlerWorkerMemoryAdmissionCompute()

    def _request(
        self,
        *,
        requested_job_memory_bytes: int,
        evaluated_at: str,
        advertisement: ModelHostMemoryAdvertisement | None = None,
    ) -> ModelMemoryAdmissionRequest:
        return ModelMemoryAdmissionRequest(
            advertisement=advertisement or _advertisement(),
            requested_job_memory_bytes=requested_job_memory_bytes,
            evaluated_at=evaluated_at,
        )

    def test_admits_when_fresh_and_within_headroom(self) -> None:
        from omnimarket.nodes.node_worker_memory_admission_compute.models import (
            EnumMemoryAdmissionOutcome,
        )

        handler = self._handler()
        request = self._request(
            requested_job_memory_bytes=4 * 1024**3,
            evaluated_at="2026-07-24T00:00:10.000000Z",  # 10s staleness, bound=60s
        )
        receipt = handler.handle(request)
        assert receipt.outcome == EnumMemoryAdmissionOutcome.ADMITTED
        assert receipt.refusal_reason is None
        usable = _TOTAL - _WIRED - _RESERVATION
        assert receipt.headroom_bytes == usable - 4 * 1024**3
        assert receipt.usable_bytes == usable

    def test_refuses_stale_advertisement_before_headroom_check(self) -> None:
        """A stale advertisement refuses even when headroom would otherwise admit."""
        from omnimarket.nodes.node_worker_memory_admission_compute.models import (
            EnumMemoryAdmissionOutcome,
            EnumMemoryAdmissionRefusalReason,
        )

        handler = self._handler()
        # cadence=30s -> staleness bound=60s; evaluated 61s after advertised.
        request = self._request(
            requested_job_memory_bytes=1,  # trivially small — would admit if fresh
            evaluated_at="2026-07-24T00:01:01.000000Z",
        )
        receipt = handler.handle(request)
        assert receipt.outcome == EnumMemoryAdmissionOutcome.REFUSED
        assert (
            receipt.refusal_reason
            == EnumMemoryAdmissionRefusalReason.STALE_ADVERTISEMENT
        )
        assert receipt.headroom_bytes is None

    def test_refuses_at_exact_staleness_bound_is_still_fresh(self) -> None:
        """staleness == bound is the inclusive fresh edge (<=), not stale."""
        from omnimarket.nodes.node_worker_memory_admission_compute.models import (
            EnumMemoryAdmissionOutcome,
        )

        handler = self._handler()
        request = self._request(
            requested_job_memory_bytes=1,
            evaluated_at="2026-07-24T00:01:00.000000Z",  # exactly 60s == bound
        )
        receipt = handler.handle(request)
        assert receipt.outcome == EnumMemoryAdmissionOutcome.ADMITTED

    def test_refuses_insufficient_headroom(self) -> None:
        from omnimarket.nodes.node_worker_memory_admission_compute.models import (
            EnumMemoryAdmissionOutcome,
            EnumMemoryAdmissionRefusalReason,
        )

        handler = self._handler()
        usable = _TOTAL - _WIRED - _RESERVATION
        request = self._request(
            requested_job_memory_bytes=usable + 1,
            evaluated_at="2026-07-24T00:00:05.000000Z",
        )
        receipt = handler.handle(request)
        assert receipt.outcome == EnumMemoryAdmissionOutcome.REFUSED
        assert (
            receipt.refusal_reason
            == EnumMemoryAdmissionRefusalReason.INSUFFICIENT_HEADROOM
        )
        assert receipt.headroom_bytes is None
        assert receipt.usable_bytes == usable

    def test_refuses_future_advertisement_clock_anomaly(self) -> None:
        """Negative staleness (advertisement stamped in the future) fails closed."""
        from omnimarket.nodes.node_worker_memory_admission_compute.models import (
            EnumMemoryAdmissionOutcome,
            EnumMemoryAdmissionRefusalReason,
        )

        handler = self._handler()
        request = self._request(
            requested_job_memory_bytes=1,
            evaluated_at="2026-07-23T23:59:00.000000Z",  # before advertised_at
        )
        receipt = handler.handle(request)
        assert receipt.outcome == EnumMemoryAdmissionOutcome.REFUSED
        assert (
            receipt.refusal_reason
            == EnumMemoryAdmissionRefusalReason.STALE_ADVERTISEMENT
        )

    def test_determinism_same_request_same_receipt(self) -> None:
        handler = self._handler()
        request = self._request(
            requested_job_memory_bytes=4 * 1024**3,
            evaluated_at="2026-07-24T00:00:10.000000Z",
        )
        first = handler.handle(request)
        second = handler.handle(request)
        assert first == second


class TestReceiptInvariants:
    def test_admitted_requires_none_refusal_reason(self) -> None:
        from omnimarket.nodes.node_worker_memory_admission_compute.models import (
            EnumMemoryAdmissionOutcome,
            EnumMemoryAdmissionRefusalReason,
            ModelMemoryAdmissionReceipt,
        )

        with pytest.raises(ValidationError):
            ModelMemoryAdmissionReceipt(
                outcome=EnumMemoryAdmissionOutcome.ADMITTED,
                host_identity="h",
                requested_job_memory_bytes=1,
                usable_bytes=10,
                headroom_bytes=9,
                staleness_seconds=1.0,
                staleness_bound_seconds=60,
                refusal_reason=EnumMemoryAdmissionRefusalReason.STALE_ADVERTISEMENT,
                evaluated_at="2026-07-24T00:00:00.000000Z",
            )

    def test_admitted_requires_non_none_headroom(self) -> None:
        from omnimarket.nodes.node_worker_memory_admission_compute.models import (
            EnumMemoryAdmissionOutcome,
            ModelMemoryAdmissionReceipt,
        )

        with pytest.raises(ValidationError):
            ModelMemoryAdmissionReceipt(
                outcome=EnumMemoryAdmissionOutcome.ADMITTED,
                host_identity="h",
                requested_job_memory_bytes=1,
                usable_bytes=10,
                headroom_bytes=None,
                staleness_seconds=1.0,
                staleness_bound_seconds=60,
                refusal_reason=None,
                evaluated_at="2026-07-24T00:00:00.000000Z",
            )

    def test_refused_requires_typed_reason_never_silent(self) -> None:
        from omnimarket.nodes.node_worker_memory_admission_compute.models import (
            EnumMemoryAdmissionOutcome,
            ModelMemoryAdmissionReceipt,
        )

        with pytest.raises(ValidationError):
            ModelMemoryAdmissionReceipt(
                outcome=EnumMemoryAdmissionOutcome.REFUSED,
                host_identity="h",
                requested_job_memory_bytes=1,
                usable_bytes=10,
                headroom_bytes=None,
                staleness_seconds=1.0,
                staleness_bound_seconds=60,
                refusal_reason=None,
                evaluated_at="2026-07-24T00:00:00.000000Z",
            )

    def test_refused_requires_none_headroom(self) -> None:
        from omnimarket.nodes.node_worker_memory_admission_compute.models import (
            EnumMemoryAdmissionOutcome,
            EnumMemoryAdmissionRefusalReason,
            ModelMemoryAdmissionReceipt,
        )

        with pytest.raises(ValidationError):
            ModelMemoryAdmissionReceipt(
                outcome=EnumMemoryAdmissionOutcome.REFUSED,
                host_identity="h",
                requested_job_memory_bytes=1,
                usable_bytes=10,
                headroom_bytes=5,
                staleness_seconds=1.0,
                staleness_bound_seconds=60,
                refusal_reason=EnumMemoryAdmissionRefusalReason.INSUFFICIENT_HEADROOM,
                evaluated_at="2026-07-24T00:00:00.000000Z",
            )


class TestMidRunCollapsePolicy:
    def test_finish_policy_never_collapses(self) -> None:
        from omnimarket.nodes.node_worker_memory_admission_compute.models import (
            should_collapse,
        )

        assert (
            should_collapse(
                current_usable_bytes=0,
                floor_bytes=1,
                collapse_policy=EnumMidRunCollapsePolicy.FINISH,
            )
            is False
        )

    def test_abort_policy_collapses_below_floor(self) -> None:
        from omnimarket.nodes.node_worker_memory_admission_compute.models import (
            should_collapse,
        )

        assert (
            should_collapse(
                current_usable_bytes=1,
                floor_bytes=2,
                collapse_policy=EnumMidRunCollapsePolicy.ABORT,
            )
            is True
        )

    def test_abort_policy_does_not_collapse_above_floor(self) -> None:
        from omnimarket.nodes.node_worker_memory_admission_compute.models import (
            should_collapse,
        )

        assert (
            should_collapse(
                current_usable_bytes=3,
                floor_bytes=2,
                collapse_policy=EnumMidRunCollapsePolicy.ABORT,
            )
            is False
        )

    def test_swap_thrash_is_not_a_declarable_policy(self) -> None:
        """The enum has exactly {finish, abort} — swap-thrash cannot be declared."""
        assert {member.value for member in EnumMidRunCollapsePolicy} == {
            "finish",
            "abort",
        }
