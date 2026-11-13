# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reader factory builds the cache from the deployment overlay and fails safe to the uncached read (OMN-19998).

Golden: an overlay naming the memory backend builds a reader that hits on the second read, and the node's example overlay is valid.
Error chain: an absent overlay, an invalid overlay and a valkey overlay whose connection variables are unset each build a reader with no
handler whose tally says why, so a step reads its source as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_source_read_cache_effect.handlers import (
    cached_source_reader_factory as factory,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader_factory import (
    build_cached_source_reader,
)
from omnimarket.nodes.node_source_read_cache_effect.models.model_source_read_cache import (
    EnumSourceCacheOutcome,
    ModelSourceCacheOverlay,
)
from tests.nodes.node_source_read_cache_effect.support import Clock, key

pytestmark = pytest.mark.unit

NODE_DIR = Path(factory.__file__).resolve().parents[1]
MEMORY = {
    "backend": "memory",
    "key_prefix": "t:",
    "ttl_backstop_seconds": 60,
    "clone_relpath_template": "{name}",
    "clone_revision_timeout_seconds": 5,
}


def _write(tmp_path: Path, doc: object) -> dict[str, str]:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return {"ONEX_SOURCE_READ_CACHE_OVERLAY": str(path)}


def _encode(value: list[int]) -> str:
    return ",".join(str(v) for v in value)


def _decode(text: str) -> list[int]:
    return [int(v) for v in text.split(",")]


def test_golden_a_memory_overlay_builds_a_reader_that_hits(tmp_path: Path) -> None:
    reader = build_cached_source_reader(_write(tmp_path, MEMORY), clock=Clock())
    calls: list[int] = []

    def fetch() -> list[int]:
        calls.append(1)
        return [1, 2]

    outcomes = [reader.read(key(), fetch, _encode, _decode)[1] for _ in range(2)]
    assert outcomes == [EnumSourceCacheOutcome.MISS, EnumSourceCacheOutcome.HIT]
    assert len(calls) == 1
    assert reader.tally.backend == "memory"


def test_golden_the_example_overlay_is_valid() -> None:
    doc = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert ModelSourceCacheOverlay.model_validate(doc).backend == "valkey"


def test_error_chain_an_absent_overlay_builds_an_uncached_reader(
    tmp_path: Path,
) -> None:
    reader = build_cached_source_reader({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert reader.tally.disabled_reason.startswith("overlay: ")
    assert (
        reader.read(key(), lambda: [1], _encode, _decode)[1]
        == EnumSourceCacheOutcome.BYPASS
    )


def test_error_chain_an_invalid_overlay_builds_an_uncached_reader(
    tmp_path: Path,
) -> None:
    env = _write(tmp_path, {**MEMORY, "ttl_backstop_seconds": 0})
    reader = build_cached_source_reader(env)
    assert reader.tally.disabled_reason.startswith("overlay: ")


def test_error_chain_a_valkey_overlay_with_unset_variables_builds_an_uncached_reader(
    tmp_path: Path,
) -> None:
    doc = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    reader = build_cached_source_reader(_write(tmp_path, doc))
    assert "host or port variable not set" in reader.tally.disabled_reason
    assert "EXAMPLE_VALKEY_HOST" in reader.tally.disabled_reason
