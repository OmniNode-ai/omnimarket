# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one-copy check's real backend: the file reader and the effect that runs the pure check over real files (OMN-19998).

Every chain test reads the chain back through the floor's own receipt fold, with the real backend named on the events. Each failing read
has its corrected control beside it, so the failing assertions can be seen to fail.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_one_copy_check_compute.handlers.handler_one_copy_check import (
    FAILURE_UNREADABLE,
    FAILURE_VIOLATION,
    NODE_NAME,
    OneCopyCheckError,
)
from omnimarket.nodes.node_one_copy_check_compute.models import (
    ModelOneCopyCheckOverlay,
    ModelOneCopyCheckRequest,
    ModelReferrerSource,
)
from omnimarket.nodes.node_one_copy_check_compute.protocols.protocol_referrer_reader import (
    ReferrerReadError,
)
from omnimarket.nodes.node_one_copy_check_effect.handlers.handler_one_copy_check_effect import (
    HandlerOneCopyCheckEffect,
)
from omnimarket.nodes.node_one_copy_check_effect.protocols.protocol_file_referrer_reader import (
    FileReferrerReader,
)

pytestmark = pytest.mark.unit

AS_OF = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
PIECE = 10
OVERLAY = ModelOneCopyCheckOverlay.model_validate(
    {
        "emission_piece": PIECE,
        "behaviours": [
            {
                "name": "lab_fill",
                "fix_landed_in": "market_copy",
                "implementations": [
                    {"id": "tick_copy", "markers": ["pkg_a.nodes.node_fill"]},
                    {"id": "market_copy", "markers": ["pkg_b.nodes.node_fill"]},
                ],
            }
        ],
        "resources": [
            {
                "name": "host_venv",
                "write_markers": ["uv pip install"],
                "lock_markers": ["staging-lock-acquire"],
                "comment_prefixes": ["#"],
            }
        ],
        "referrer_sources": [
            {
                "id": "unit:fill-tick",
                "kind": "unit",
                "path": "pkg_b/units.yaml",
                "yaml_key": "units.fill-tick",
            },
            {"id": "tick:throughput", "kind": "tick", "path": "pkg_a/tick.py"},
        ],
    }
)


def _clock() -> Callable[[], datetime]:
    state = {"n": 0}

    def tick() -> datetime:
        state["n"] += 1
        return AS_OF + timedelta(seconds=state["n"])

    return tick


def _fold(sink: InMemoryEmissionSink) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=AS_OF + timedelta(minutes=5),
            emissions=tuple(sink.events),
            pieces=(
                ModelPieceDef(
                    piece=PIECE, name="one-copy-check", reading_max_age_s=900
                ),
            ),
            joins=(),
            freshness_max_age_s=900,
            error_max_age_s=900,
            chain_deadline_s=300,
        )
    )


def _write(root: Path, tick_text: str) -> None:
    (root / "pkg_a").mkdir(parents=True, exist_ok=True)
    (root / "pkg_b").mkdir(parents=True, exist_ok=True)
    (root / "pkg_a" / "tick.py").write_text(tick_text, encoding="utf-8")
    (root / "pkg_b" / "units.yaml").write_text(
        "units:\n  fill-tick:\n    module: pkg_b.nodes.node_fill.tick\n",
        encoding="utf-8",
    )


def _handler(root: Path, sink: InMemoryEmissionSink) -> HandlerOneCopyCheckEffect:
    return HandlerOneCopyCheckEffect(root, OVERLAY, _clock(), sink)


def test_file_reader_reads_plain_text_and_yaml_subtrees(
    tmp_path: Path,
) -> None:
    tick_path = tmp_path / "pkg_a" / "tick.py"
    tick_path.parent.mkdir(parents=True)
    tick_path.write_text(
        'importlib.import_module("pkg_a.nodes.node_fill.handler")\n', encoding="utf-8"
    )
    units_path = tmp_path / "pkg_b" / "units.yaml"
    units_path.parent.mkdir(parents=True)
    units_path.write_text(
        "units:\n  fill-tick: python -m pkg_b.nodes.node_fill.tick\n", encoding="utf-8"
    )
    sources = [
        ModelReferrerSource(id="tick:throughput", kind="tick", path="pkg_a/tick.py"),
        ModelReferrerSource(
            id="unit:fill-tick",
            kind="unit",
            path="pkg_b/units.yaml",
            yaml_key="units.fill-tick",
        ),
    ]

    referrers = FileReferrerReader(tmp_path, sources).read()

    assert [referrer.id for referrer in referrers] == [
        "tick:throughput",
        "unit:fill-tick",
    ]
    assert (
        referrers[0].text
        == 'importlib.import_module("pkg_a.nodes.node_fill.handler")\n'
    )
    assert "pkg_b.nodes.node_fill" in referrers[1].text


def test_file_reader_a_missing_file_and_a_missing_yaml_key_are_typed_refusals(
    tmp_path: Path,
) -> None:
    units_path = tmp_path / "pkg_b" / "units.yaml"
    units_path.parent.mkdir(parents=True)
    units_path.write_text(
        "units:\n  fill-tick: python -m pkg_b.nodes.node_fill.tick\n", encoding="utf-8"
    )
    missing_file = ModelReferrerSource(
        id="unit:absent", kind="unit", path="pkg_a/absent.yaml"
    )
    missing_key = ModelReferrerSource(
        id="unit:fill-tick",
        kind="unit",
        path="pkg_b/units.yaml",
        yaml_key="units.ghost-key",
    )

    with pytest.raises(ReferrerReadError, match="unit:absent"):
        FileReferrerReader(tmp_path, [missing_file]).read()
    with pytest.raises(ReferrerReadError, match=r"units\.ghost-key"):
        FileReferrerReader(tmp_path, [missing_key]).read()


# ------------------------------------------------------------------------------------------------ the effect on real files


def test_golden_chain_the_effect_reads_real_files_and_a_clean_check_is_golden_on_the_real_backend(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "print('no copy here')\n")
    sink = InMemoryEmissionSink()

    result = _handler(tmp_path, sink).handle(ModelOneCopyCheckRequest(as_of=AS_OF))

    assert result.ok, result.findings
    assert [event.phase for event in sink.events] == ["start", "success"]
    assert {event.node for event in sink.events} == {NODE_NAME}
    assert {event.backend for event in sink.events} == {"real"}
    (chain,) = _fold(sink).chains
    assert chain.kind == "golden"


def test_error_chain_two_copies_in_real_files_fail_the_chain_and_the_corrected_file_is_golden(
    tmp_path: Path,
) -> None:
    _write(tmp_path, 'importlib.import_module("pkg_a.nodes.node_fill.handler")\n')
    sink = InMemoryEmissionSink()

    result = _handler(tmp_path, sink).handle(ModelOneCopyCheckRequest(as_of=AS_OF))

    assert not result.ok
    assert [finding.code for finding in result.findings] == ["multiple_live"]
    assert sink.events[-1].phase == "failure"
    assert sink.events[-1].failure_code == FAILURE_VIOLATION
    (chain,) = _fold(sink).chains
    assert chain.kind == "error"

    # Control: the tick's import removed, the same effect on the same files reads golden.
    _write(tmp_path, "print('no copy here')\n")
    clean = InMemoryEmissionSink()
    assert _handler(tmp_path, clean).handle(ModelOneCopyCheckRequest(as_of=AS_OF)).ok
    assert [chain.kind for chain in _fold(clean).chains] == ["golden"]


def test_error_chain_a_source_file_that_is_missing_is_a_typed_unreadable_failure_not_a_clean_check(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "print('no copy here')\n")
    (tmp_path / "pkg_a" / "tick.py").unlink()
    sink = InMemoryEmissionSink()

    with pytest.raises(OneCopyCheckError) as raised:
        _handler(tmp_path, sink).handle(ModelOneCopyCheckRequest(as_of=AS_OF))

    assert raised.value.code == FAILURE_UNREADABLE
    assert [event.phase for event in sink.events] == ["start", "failure"]
    (chain,) = _fold(sink).chains
    assert chain.kind == "error"
