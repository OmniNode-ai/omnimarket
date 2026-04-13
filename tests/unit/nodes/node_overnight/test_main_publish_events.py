# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_overnight __main__ --publish-events flag (OMN-8403).

Verifies that:
- _build_kafka_publisher() returns None when KAFKA_BOOTSTRAP_SERVERS is unset.
- _build_kafka_publisher() returns None when confluent_kafka is not importable.
- _build_kafka_publisher() returns a callable when the env var is set and
  the producer can be constructed (mocked Producer).
- The callable forwards (topic, payload) to Producer.produce.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
def test_build_kafka_publisher_returns_none_when_no_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)

    from omnimarket.nodes.node_overnight.__main__ import _build_kafka_publisher

    result = _build_kafka_publisher()
    assert result is None


@pytest.mark.unit
def test_build_kafka_publisher_returns_none_when_confluent_kafka_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "192.168.86.201:19092")

    # Hide confluent_kafka from imports
    with patch.dict(sys.modules, {"confluent_kafka": None}):  # type: ignore[dict-item]
        # Re-import to get a fresh execution of the import guard
        import importlib

        import omnimarket.nodes.node_overnight.__main__ as main_mod

        importlib.reload(main_mod)
        result = main_mod._build_kafka_publisher()

    assert result is None


@pytest.mark.unit
def test_build_kafka_publisher_returns_callable_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "192.168.86.201:19092")

    mock_producer = MagicMock()
    mock_confluent = MagicMock()
    mock_confluent.Producer.return_value = mock_producer

    with patch.dict(sys.modules, {"confluent_kafka": mock_confluent}):
        import importlib

        import omnimarket.nodes.node_overnight.__main__ as main_mod

        importlib.reload(main_mod)
        publisher = main_mod._build_kafka_publisher()

    assert publisher is not None
    assert callable(publisher)


@pytest.mark.unit
def test_build_kafka_publisher_callable_invokes_produce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "192.168.86.201:19092")

    mock_producer = MagicMock()
    mock_confluent = MagicMock()
    mock_confluent.Producer.return_value = mock_producer

    with patch.dict(sys.modules, {"confluent_kafka": mock_confluent}):
        import importlib

        import omnimarket.nodes.node_overnight.__main__ as main_mod

        importlib.reload(main_mod)
        publisher = main_mod._build_kafka_publisher()

    assert publisher is not None
    payload = b'{"test": true}'
    publisher("onex.evt.omnimarket.overnight-session-completed.v1", payload)

    mock_producer.produce.assert_called_once_with(
        "onex.evt.omnimarket.overnight-session-completed.v1",
        value=payload,
    )
    mock_producer.poll.assert_called_once_with(0)
