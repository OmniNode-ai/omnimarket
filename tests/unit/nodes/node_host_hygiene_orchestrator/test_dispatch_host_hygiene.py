# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the host hygiene dispatch handler."""

from __future__ import annotations

from omnimarket.models.model_host_hygiene import ModelHostHygieneDecisionItem
from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneUnitOutcome,
    ModelHostHygieneOrchestratorOverlay,
)
from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.dispatch_host_hygiene import (
    DispatchHostHygieneUnits,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.sender_in_memory import (
    InMemoryHostHygieneSender,
)


def _overlay() -> ModelHostHygieneOrchestratorOverlay:
    return ModelHostHygieneOrchestratorOverlay.model_validate(
        {
            "job_id": "host_hygiene",
            "run_interval_seconds": 300,
            "grace_seconds": 60,
            "backend": "in_memory",
            "max_reading_age_seconds": 3600,
            "max_parallel_units": 4,
            "lab_work": {
                "repo": "OmniNode-ai/omnimarket",
                "commit_sha": "b" * 40,
                "lane": "hygiene-lane",
                "unit_id_prefix": "hh",
                "bus_lane": "bus-1",
            },
            "cleanups": [
                {
                    "cleanup_id": "uv_cache",
                    "min_used_fraction": 0.8,
                    "min_interval_seconds": 3600,
                    "when_unread": "dispatch",
                    "argv": [
                        "python3",
                        "-m",
                        "example.cleanup",
                        "--omni-home",
                        "{registry_root}",
                        "--host",
                        "{host}",
                    ],
                    "timeout_seconds": 600,
                    "kind": "other",
                },
                {
                    "cleanup_id": "docker_images",
                    "min_used_fraction": 0.9,
                    "min_interval_seconds": 7200,
                    "when_unread": "skip",
                    "argv": ["docker", "system", "prune", "--host", "{host}"],
                    "timeout_seconds": 1200,
                    "kind": "other",
                },
            ],
            "hosts": [
                {
                    "name": "host-a",
                    "registry_root": "/home/omni",
                    "pressure_volume": "/",
                    "cleanup_ids": ["uv_cache", "docker_images"],
                }
            ],
        }
    )


def _due_items() -> tuple[ModelHostHygieneDecisionItem, ...]:
    return (
        ModelHostHygieneDecisionItem(
            host="host-a",
            cleanup_id="uv_cache",
            due=True,
            reason="disk_pressure",
        ),
        ModelHostHygieneDecisionItem(
            host="host-a",
            cleanup_id="docker_images",
            due=True,
            reason="disk_pressure",
        ),
    )


def test_two_due_items_become_two_units_with_placeholders_replaced() -> None:
    overlay = _overlay()
    sender = InMemoryHostHygieneSender()
    dispatcher = DispatchHostHygieneUnits(overlay=overlay, sender=sender)
    results = dispatcher.dispatch(_due_items())
    assert len(results) == 2
    assert len(sender.sent) == 2
    # Check argv placeholders replaced
    unit1 = sender.sent[0]
    assert "host-a" in unit1.argv
    assert "/home/omni" in unit1.argv  # {registry_root} replaced by the host row
    assert "{host}" not in " ".join(unit1.argv)
    assert "{registry_root}" not in " ".join(unit1.argv)
    # Check repo/sha/lane from overlay
    assert unit1.repo == "OmniNode-ai/omnimarket"
    assert unit1.commit_sha == "b" * 40
    assert unit1.lane == "hygiene-lane"


def test_receipt_mapping_completed_exit_0() -> None:
    overlay = _overlay()
    sender = InMemoryHostHygieneSender(
        scripted={"host-a": (EnumLabWorkUnitStatus.COMPLETED, 0, "ok")}
    )
    dispatcher = DispatchHostHygieneUnits(overlay=overlay, sender=sender)
    results = dispatcher.dispatch(_due_items())
    assert all(r.outcome is EnumHostHygieneUnitOutcome.COMPLETED for r in results)


def test_receipt_mapping_completed_exit_3_is_failed() -> None:
    overlay = _overlay()
    sender = InMemoryHostHygieneSender(
        scripted={"host-a": (EnumLabWorkUnitStatus.COMPLETED, 3, "error output")}
    )
    dispatcher = DispatchHostHygieneUnits(overlay=overlay, sender=sender)
    results = dispatcher.dispatch(_due_items())
    assert all(r.outcome is EnumHostHygieneUnitOutcome.FAILED for r in results)


def test_receipt_mapping_timed_out() -> None:
    overlay = _overlay()
    sender = InMemoryHostHygieneSender(
        scripted={"host-a": (EnumLabWorkUnitStatus.TIMED_OUT, None, "")}
    )
    dispatcher = DispatchHostHygieneUnits(overlay=overlay, sender=sender)
    results = dispatcher.dispatch(_due_items())
    assert all(r.outcome is EnumHostHygieneUnitOutcome.TIMED_OUT for r in results)


def test_receipt_mapping_refused() -> None:
    overlay = _overlay()
    sender = InMemoryHostHygieneSender(
        scripted={"host-a": (EnumLabWorkUnitStatus.REFUSED, None, "refused")}
    )
    dispatcher = DispatchHostHygieneUnits(overlay=overlay, sender=sender)
    results = dispatcher.dispatch(_due_items())
    assert all(r.outcome is EnumHostHygieneUnitOutcome.REFUSED for r in results)


def test_receipt_mapping_infra_error() -> None:
    overlay = _overlay()
    sender = InMemoryHostHygieneSender(
        scripted={"host-a": (EnumLabWorkUnitStatus.INFRA_ERROR, None, "no terminal")}
    )
    dispatcher = DispatchHostHygieneUnits(overlay=overlay, sender=sender)
    results = dispatcher.dispatch(_due_items())
    assert all(
        r.outcome is EnumHostHygieneUnitOutcome.TRANSPORT_UNAVAILABLE for r in results
    )


def test_raising_sender_yields_transport_unavailable() -> None:
    overlay = _overlay()

    class RaisingSender:
        def __init__(self) -> None:
            self.sent: list[ModelLabWorkUnitRequest] = []

        def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
            self.sent.append(request)
            raise RuntimeError("connection refused")

    sender = RaisingSender()
    dispatcher = DispatchHostHygieneUnits(overlay=overlay, sender=sender)
    results = dispatcher.dispatch(_due_items())
    assert all(
        r.outcome is EnumHostHygieneUnitOutcome.TRANSPORT_UNAVAILABLE for r in results
    )
    assert "RuntimeError" in results[0].detail


def test_undeclared_host_refused_with_no_send() -> None:
    overlay = _overlay()
    sender = InMemoryHostHygieneSender()
    dispatcher = DispatchHostHygieneUnits(overlay=overlay, sender=sender)
    due = (
        ModelHostHygieneDecisionItem(
            host="unknown-host",
            cleanup_id="uv_cache",
            due=True,
            reason="disk_pressure",
        ),
    )
    results = dispatcher.dispatch(due)
    assert results[0].outcome is EnumHostHygieneUnitOutcome.REFUSED
    assert "unknown-host" in results[0].detail
    assert sender.sent == []


def test_parallel_order_preserved() -> None:
    overlay = _overlay()
    sender = InMemoryHostHygieneSender()
    dispatcher = DispatchHostHygieneUnits(overlay=overlay, sender=sender)
    results = dispatcher.dispatch(_due_items())
    # Results in input order
    assert results[0].cleanup_id == "uv_cache"
    assert results[1].cleanup_id == "docker_images"
