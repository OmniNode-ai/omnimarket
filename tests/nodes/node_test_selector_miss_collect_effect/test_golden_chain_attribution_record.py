# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the attribution record publication, through the packaged contract."""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.test_selector_reading import EnumFileOutcome
from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.inmemory_adapters import (
    InMemoryAlarmReceiver,
    InMemoryAlarmStore,
    InMemoryNightlyGateway,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.inmemory_record_adapters import (
    InMemoryRecordGateway,
    InMemoryRecordStore,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_attribution_record import (
    EnumPublishStatus,
    ModelPublishedAttributionRecord,
    ModelPublishRecordRequest,
    PublicSafetyError,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ModelCollectRequest,
    ModelMissCollectSettings,
    ModelNightlyRun,
    ModelNightReading,
    ModelPreviousNight,
    load_policy,
)

pytestmark = pytest.mark.unit

NODE = "omnimarket.nodes.node_test_selector_miss_collect_effect"
SLOT = date(2026, 11, 21)
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
HEAD = "c" * 40
OVERLAY: dict[str, Any] = {
    "repo": "example-org/example-repo",
    "workflow_file": "nightly-full-suite.yml",
    "owner_role": "example-test-selection-owner",
    "alarm_route": "test_selector_miss",
    "collector_enabled": True,
    "status_tick": "example-lab-status-tick",
    "public_repositories": ["example-org/example-repo"],
    "private_terms": ["example-private-term"],
}


def _entry() -> dict[str, Any]:
    package = importlib.import_module(NODE)
    assert package.__file__ is not None
    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    return {e["operation"]: e for e in contract["cli_handlers"]}[
        "publish_attribution_record"
    ]


def _wired(
    overlay: dict[str, Any],
) -> tuple[Any, Any, InMemoryRecordGateway, InMemoryRecordStore]:
    policy = load_policy()
    settings = ModelMissCollectSettings.load(overlay)
    files = {
        f"tests/bulk/test_{n}.py": EnumFileOutcome.PASSED
        for n in range(policy.nightly_min_test_files)
    }
    night = InMemoryNightlyGateway(
        run=ModelNightlyRun(run_id=42, completed=True, head_sha=HEAD),
        reading=ModelNightReading(
            artifacts_found=policy.nightly_shard_count,
            shards_in_error=0,
            outcomes=files,
        ),
        previous=ModelPreviousNight(head_sha="d" * 40, outcomes=files),
    )
    alarms = InMemoryAlarmStore()
    collect_class = importlib.import_module(
        f"{NODE}.handlers.handler_collect_nightly"
    ).HandlerCollectNightly
    collected = collect_class(
        settings=settings,
        policy=policy,
        gateway=night,
        store=alarms,
        receiver=InMemoryAlarmReceiver(),
    ).handle(ModelCollectRequest(slot_date=SLOT, now=NOW))
    records, gateway = InMemoryRecordStore(), InMemoryRecordGateway()
    entry = _entry()
    handler = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )(
        settings=settings,
        policy=policy,
        night=night,
        alarms=alarms,
        records=records,
        gateway=gateway,
    )
    return handler, collected, gateway, records


def _request(collected: Any) -> ModelPublishRecordRequest:
    return ModelPublishRecordRequest(
        slot_date=SLOT,
        now=NOW,
        collect_result=collected,
        selector_version="1.0.0",
        policy_version="1",
        stage_entry_counted=0,
    )


def test_golden_a_clean_night_publishes_one_clean_record_through_the_contract_handler() -> (
    None
):
    handler, collected, gateway, _ = _wired(OVERLAY)
    result = handler.handle(_request(collected))
    assert result.status is EnumPublishStatus.PUBLISHED
    assert isinstance(result.record, ModelPublishedAttributionRecord)
    assert result.record.clean
    assert result.record.nightly_run_id == 42
    assert result.record.tested_head_sha == HEAD
    ((repo, check_run),) = gateway.check_runs
    assert repo == "example-org/example-repo"
    assert check_run.head_sha == HEAD
    assert check_run.conclusion == "success"


def test_error_chain_a_private_repository_is_refused_before_any_write() -> None:
    handler, collected, gateway, records = _wired(
        {**OVERLAY, "public_repositories": ["example-org/another-repo"]}
    )
    with pytest.raises(PublicSafetyError) as raised:
        handler.handle(_request(collected))
    assert raised.value.code == "public_safety_refused"
    assert gateway.check_runs == []
    assert records.rows == {}


def test_error_chain_an_overlay_without_an_owner_role_never_builds_the_handler() -> (
    None
):
    from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
        OwnerRoleUnsetError,
    )

    document = {k: v for k, v in OVERLAY.items() if k != "owner_role"}
    with pytest.raises(OwnerRoleUnsetError):
        ModelMissCollectSettings.load(document)


def test_error_chain_a_request_without_a_zone_aware_time_is_refused() -> None:
    _, collected, _, _ = _wired(OVERLAY)
    with pytest.raises(ValueError, match="timezone"):
        ModelPublishRecordRequest.model_validate(
            {
                "slot_date": SLOT,
                "now": "2026-11-22T12:00:00",
                "collect_result": collected,
                "selector_version": "1.0.0",
                "policy_version": "1",
                "stage_entry_counted": 0,
            }
        )
