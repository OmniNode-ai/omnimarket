# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The publish stamps the record's data_health with the publish time, apart from the time the record was generated."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishRequest,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    RECORD,
    LocalSsh,
    _overlay,
)

pytestmark = pytest.mark.unit

PUBLISHED = datetime(2026, 10, 31, 12, 7, 0, tzinfo=UTC)


def _publish(tmp_path: Path, record: dict[str, object]) -> dict[str, object]:
    home = tmp_path / "home"
    target = home / load_runtime().source.relpath
    target.parent.mkdir(parents=True)
    raw = json.dumps(record)
    target.write_text(raw, encoding="utf-8")
    request = ModelFactoryFloorPublishRequest(
        home=str(home), overlay=_overlay(tmp_path), skip_refresh=True, now=PUBLISHED
    )
    result = HandlerFactoryFloorPublish(LocalSsh()).handle(request, environ={})
    served = (tmp_path / "site" / "factory-floor.json").read_bytes()
    assert result.data_sha256 == hashlib.sha256(served).hexdigest()
    return cast("dict[str, object]", json.loads(served))


def test_published_at_is_stamped_apart_from_generated_at(tmp_path: Path) -> None:
    record = {
        **RECORD,
        "data_health": {
            "generated_at": RECORD["generated_at"],
            "published_at": None,
            "sources": [],
        },
    }
    served = _publish(tmp_path, record)
    health = served["data_health"]
    assert isinstance(health, dict)
    assert health["published_at"] == "2026-10-31T12:07:00+00:00"
    assert health["generated_at"] == RECORD["generated_at"]
    assert health["published_at"] != health["generated_at"]
    assert served["generated_at"] == RECORD["generated_at"]
    assert served["lines"] == RECORD["lines"]


def test_a_record_without_data_health_is_published_unchanged(tmp_path: Path) -> None:
    served = _publish(tmp_path, dict(RECORD))
    assert served == RECORD
