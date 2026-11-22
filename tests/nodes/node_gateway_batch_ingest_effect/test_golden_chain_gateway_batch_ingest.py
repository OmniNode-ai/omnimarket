# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of node_gateway_batch_ingest_effect: the contract the runtime discovers, then a token, a batch and a produce.

Uses the real contract.yaml, a real overlay document and the real lab JWKS verifier; the producer is a recording
double, so no broker is needed.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from omnibase_infra.runtime.auto_wiring import (
    discover_contracts_from_paths,
    filter_manifest_for_runtime_profile,
)

from omnimarket.nodes.node_gateway_batch_ingest_effect import asgi
from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    PREFIX,
    TOPIC_A,
    FakePublisher,
    batch,
    mint,
    overlay_document,
    post,
    record,
)

CONTRACT = Path(asgi.__file__).with_name("contract.yaml")
PROOF_SLUG = "cli-run-h051"
PROOF_TENANT = "33333333-3333-4333-8333-333333333333"


def test_golden_chain_gateway_batch_ingest_contract_is_discovered_for_the_effects_profile() -> (
    None
):
    manifest = discover_contracts_from_paths([CONTRACT])
    assert manifest.total_errors == 0
    assert manifest.total_discovered == 1
    effects = filter_manifest_for_runtime_profile(manifest, "effects")
    assert [c.name for c in effects.manifest.contracts] == [
        "node_gateway_batch_ingest_effect"
    ]
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["http_mount"]["path_prefix"] == PREFIX


async def test_golden_chain_gateway_batch_ingest_token_to_wire_topic() -> None:
    document = overlay_document(
        chunk_headroom_bytes=asgi.mount_ceiling()[1] + 1,
        max_inflight_bytes=8_000_000,
    )
    document["tenants"].append(
        {"tenant_id": PROOF_TENANT, "tenant_slug": PROOF_SLUG, "status": "active"}
    )
    overlay = ModelBatchIngestOverlay.model_validate(document)
    publisher = FakePublisher(
        message_max_bytes=overlay.kafka.producer_max_request_bytes
    )
    app = asgi.create_app(overlay=overlay, publisher=publisher)
    token = mint(tenant_id=PROOF_TENANT, slug=PROOF_SLUG)
    result = await post(app, batch(record(TOPIC_A, b'{"run_id":"r1"}')), token=token)
    assert (result.status, result.body) == (202, {"published": 1})
    [produced] = publisher.calls
    assert produced.topic == f"tenant-{PROOF_SLUG}.{TOPIC_A}"
    assert dict(produced.headers)["onex-tenant-slug"] == PROOF_SLUG.encode()
    assert dict(produced.headers)["message-id"] == produced.event_id.encode()
