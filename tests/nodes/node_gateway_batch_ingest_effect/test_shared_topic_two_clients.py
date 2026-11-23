# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC18: a record built by the merged `PublisherHttps` carries the id the door derives, over a real TLS loopback socket."""

from __future__ import annotations

from pathlib import Path

from omnibase_core.models.contracts.model_ingest_contract import compute_event_id

from omnimarket.nodes.node_gateway_batch_ingest_effect.event_identity import (
    derive_event_id,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    TOPIC_A,
    make_app,
    mint,
)
from tests.nodes.node_gateway_batch_ingest_effect.test_header_budget_real_client import (
    _loopback_door,
    _sender,
)


def test_shared_topic_publisher_https_record_carries_the_door_event_id(
    tmp_path: Path,
) -> None:
    app, publisher = make_app()
    key, value, headers = b"k-1", b'{"n":2}', {"x-a": "1"}
    with _loopback_door(app, tmp_path) as (url, verify):
        sender = _sender(url, verify, mint())
        sender.publish(TOPIC_A, key, value, headers)
        assert sender.flush(10) == 0
    assert sender.failure_detail() is None
    [call] = publisher.calls
    sent_id = compute_event_id(
        TOPIC_A, key, value, [(name, text.encode()) for name, text in headers.items()]
    )
    assert call.event_id == sent_id
    assert call.event_id == derive_event_id(
        TOPIC_A, key, {name: text.encode() for name, text in headers.items()}, value
    )
