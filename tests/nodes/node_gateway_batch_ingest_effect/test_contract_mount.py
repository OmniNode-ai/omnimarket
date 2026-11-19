# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC20: the mount is declared in contract.yaml, is the only serving path, and bounds are overlay values."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml
from omnibase_infra.models.model_http_mount import ModelHttpMount

from omnimarket.nodes.node_gateway_batch_ingest_effect import asgi
from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
    load_overlay,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    batch,
    make_app,
    make_overlay,
    overlay_document,
    post,
    record,
)

NODE = Path(asgi.__file__).parent


def _contract() -> dict[str, object]:
    return yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))


def test_contract_mount_declares_the_http_mount_the_runtime_loads() -> None:
    block = dict(_contract()["http_mount"])  # type: ignore[call-overload]
    assert block["max_body_bytes"] == asgi.mount_ceiling()[1]
    if "max_body_bytes" not in ModelHttpMount.model_fields:
        # The locked omnibase-infra predates the runtime body cap (omnibase_infra#6079): its loader would refuse the
        # key, so the rest of the block is read through it and the ceiling is asserted from the contract above.
        del block["max_body_bytes"]
    mount = ModelHttpMount.from_contract_block(block)
    assert mount is not None
    assert mount.path_prefix == "/v1/gateway/batch-ingest"
    assert (
        mount.factory
        == "omnimarket.nodes.node_gateway_batch_ingest_effect.asgi:create_app"
    )
    module, _, attribute = mount.factory.partition(":")
    assert callable(getattr(importlib.import_module(module), attribute))


def test_contract_mount_removing_the_http_mount_block_fails_the_loader() -> None:
    contract = _contract()
    del contract["http_mount"]
    assert ModelHttpMount.from_contract_block(contract.get("http_mount")) is None


def test_contract_mount_is_the_only_serving_path() -> None:
    contract = _contract()
    assert contract.get("http_ingress") is None
    assert contract["event_bus"]["subscribe_topics"] == []  # type: ignore[index]


def test_contract_mount_declares_every_overlay_topic() -> None:
    declared = set(_contract()["event_bus"]["publish_topics"])  # type: ignore[index]
    example = yaml.safe_load(
        (NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert {t["topic"] for t in example["topics"]} <= declared


def test_contract_mount_the_example_overlay_validates_against_the_model() -> None:
    example = yaml.safe_load(
        (NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    overlay = ModelBatchIngestOverlay.model_validate(example)
    assert overlay.limits.chunk_headroom_bytes > asgi.mount_ceiling()[1]


def test_contract_mount_factory_builds_the_app_from_the_overlay_file(
    tmp_path: Path,
) -> None:
    document = overlay_document(
        chunk_headroom_bytes=asgi.mount_ceiling()[1] + 1, max_inflight_bytes=8_000_000
    )
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    environ = {"GATEWAY_BATCH_INGEST_OVERLAY_FILE": str(path)}
    assert load_overlay(environ).kafka.lane == "dev"
    app = asgi.create_app(environ=environ)
    assert isinstance(app, asgi.BatchIngestApp)


def test_contract_mount_refuses_an_overlay_body_bound_above_the_mount_ceiling() -> None:
    document = overlay_document(
        max_body_bytes=asgi.mount_ceiling()[1] + 1, max_inflight_bytes=9_000_000
    )
    with pytest.raises(ValueError, match="http_mount"):
        asgi.create_app(overlay=ModelBatchIngestOverlay.model_validate(document))


@pytest.mark.parametrize(
    ("bound", "count", "status"),
    [(1, 1, 202), (1, 2, 422), (100, 100, 202), (100, 101, 422), (101, 101, 202)],
)
async def test_overlay_bounds_move_the_refusal_point_with_no_code_change(
    bound: int, count: int, status: int
) -> None:
    app, _ = make_app(
        make_overlay(max_batch_records=bound, max_body_bytes=60000, max_value_chars=100)
    )
    records = [record(value=f"r{i}".encode()) for i in range(count)]
    result = await post(app, batch(*records))
    assert result.status == status
    if status == 422:
        assert result.body["error"] == "batch-too-large"
