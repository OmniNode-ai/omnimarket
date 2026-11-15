# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One behaviour, one live implementation; one resource, every writer under its lock (OMN-19998).

Each test decides over in-memory referrers and reads the chain back through the floor's own receipt
fold. Each error test has a control beside it: the same read without the injected break or with the
corrected referrer text must read golden, which shows the failure assertions can fail.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_one_copy_check_compute.handlers.handler_one_copy_check import (
    FAILURE_SILENT,
    FAILURE_UNREADABLE,
    FAILURE_VIOLATION,
    NODE_NAME,
    HandlerOneCopyCheck,
    OneCopyCheckError,
    load_overlay,
)
from omnimarket.nodes.node_one_copy_check_compute.models import (
    ModelOneCopyCheckOverlay,
    ModelOneCopyCheckRequest,
    ModelOneCopyCheckResult,
    ModelReferrer,
)
from omnimarket.nodes.node_one_copy_check_compute.protocols.protocol_referrer_reader import (
    EnumReferrerBreak,
    InMemoryReferrerReader,
    ProtocolReferrerReader,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

AS_OF = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
PIECE = 10
OVERLAY_FIELDS: dict[str, object] = {
    "emission_piece": PIECE,
    "behaviours": [
        {
            "name": "lab_fill",
            "fix_landed_in": "market_copy",
            "implementations": [
                {"id": "tick_copy", "markers": ["pkg_a.nodes.node_fill"]},
                {"id": "market_copy", "markers": ["pkg_b.nodes.node_fill"]},
                {"id": "idle_copy", "markers": ["pkg_c.nodes.node_fill"]},
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
        {"id": "source:reconcile", "kind": "source", "path": "pkg_a/reconcile.sh"},
    ],
}
#: The example overlay the package ships, as the suite supplies it.
EXAMPLE_OVERLAY_FIELDS: dict[str, object] = {
    "emission_piece": 10,
    "behaviours": [
        {
            "name": "example_fill",
            "fix_landed_in": "market_copy",
            "implementations": [
                {"id": "tick_copy", "markers": ["pkg_a.nodes.node_example_fill"]},
                {"id": "market_copy", "markers": ["pkg_b.nodes.node_example_fill"]},
            ],
        }
    ],
    "resources": [
        {
            "name": "example_host_venv",
            "write_markers": ["uv pip install --python"],
            "lock_markers": ["staging-lock-acquire"],
            "comment_prefixes": ["#"],
        }
    ],
    "referrer_sources": [
        {
            "id": "unit:example-fill-tick",
            "kind": "unit",
            "path": "pkg_b/units.yaml",
            "yaml_key": "units.example-fill-tick",
        },
        {"id": "tick:example-tick", "kind": "tick", "path": "pkg_a/tick.py"},
        {
            "id": "writer:example-reconcile",
            "kind": "source",
            "path": "pkg_a/reconcile.sh",
        },
    ],
}
UNIT_REFERRER = ModelReferrer(
    id="unit:fill-tick", kind="unit", text="python -m pkg_b.nodes.node_fill.tick"
)
TICK_REFERRER = ModelReferrer(
    id="tick:throughput",
    kind="tick",
    text='importlib.import_module("pkg_a.nodes.node_fill.handler")',
)


def _overlay() -> ModelOneCopyCheckOverlay:
    return ModelOneCopyCheckOverlay.model_validate(OVERLAY_FIELDS)


def _clock() -> Callable[[], datetime]:
    state = {"n": 0}

    def tick() -> datetime:
        state["n"] += 1
        return AS_OF + timedelta(seconds=state["n"])

    return tick


def _handler(
    reader: ProtocolReferrerReader,
    sink: InMemoryEmissionSink,
    overlay: ModelOneCopyCheckOverlay | None = None,
) -> HandlerOneCopyCheck:
    return HandlerOneCopyCheck(overlay or _overlay(), reader, _clock(), sink)


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


def _decide(referrers: list[ModelReferrer]) -> ModelOneCopyCheckResult:
    return _handler(InMemoryReferrerReader(referrers), InMemoryEmissionSink()).handle(
        ModelOneCopyCheckRequest(as_of=AS_OF)
    )


# ---------------------------------------------------------------------------------------------------- behaviour
def test_unit_and_tick_running_two_copies_is_a_violation() -> None:
    # The failing control every later test corrects: both copies are run and idle_copy runs for nobody.
    result = _decide([UNIT_REFERRER, TICK_REFERRER])

    assert result.ok is False
    assert [finding.code for finding in result.findings] == ["multiple_live"]
    finding = result.findings[0]
    assert "tick_copy" in finding.detail
    assert "market_copy" in finding.detail
    assert result.behaviours[0].unreferenced == ("idle_copy",)


def test_the_tick_that_runs_no_copy_leaves_market_copy_the_only_live_one() -> None:
    # Control for the multiple_live test above: the same read with the tick's import corrected.
    quiet_tick = ModelReferrer(
        id="tick:throughput", kind="tick", text='print("throughput")'
    )

    result = _decide([UNIT_REFERRER, quiet_tick])

    assert result.ok is True
    assert result.findings == ()
    assert tuple(live.id for live in result.behaviours[0].live) == ("market_copy",)
    assert result.behaviours[0].unreferenced == ("idle_copy", "tick_copy")


def test_only_the_tick_running_the_copy_the_fix_is_not_in_is_fix_not_live() -> None:
    result = _decide([TICK_REFERRER])

    assert {finding.code for finding in result.findings} == {"fix_not_live"}
    assert tuple(live.id for live in result.behaviours[0].live) == ("tick_copy",)


def test_referrers_that_name_no_copy_are_none_live() -> None:
    quiet_unit = ModelReferrer(
        id="unit:fill-tick", kind="unit", text="python -m pkg_b.cli"
    )
    quiet_tick = ModelReferrer(
        id="tick:throughput", kind="tick", text='print("throughput")'
    )

    result = _decide([quiet_unit, quiet_tick])

    assert result.ok is False
    assert {"none_live"} <= {finding.code for finding in result.findings}
    assert result.behaviours[0].live == ()


def test_a_source_referrer_is_not_a_running_kind_so_it_makes_nothing_live() -> None:
    source = ModelReferrer(
        id="source:reconcile", kind="source", text="run pkg_a.nodes.node_fill.handler"
    )

    result = _decide([source])

    assert result.behaviours[0].live == ()
    assert {"none_live"} <= {finding.code for finding in result.findings}


# ---------------------------------------------------------------------------------------------------- resource
def test_a_writer_that_takes_no_lock_marker_is_a_finding() -> None:
    writer = ModelReferrer(
        id="writer:x", kind="source", text="uv pip install --python x"
    )

    result = _decide([UNIT_REFERRER, writer])

    assert result.ok is False
    assert "writer_without_lock" in {finding.code for finding in result.findings}
    assert result.resources[0].writers == ("writer:x",)
    assert result.resources[0].writers_without_lock == ("writer:x",)


def test_the_same_writer_holding_the_lock_marker_is_clean() -> None:
    # Only the resource part varies: the behaviour is decided by the corrected unit referrer alone.
    writer = ModelReferrer(
        id="writer:x",
        kind="source",
        text="uv pip install --python x\nstaging-lock-acquire\n",
    )

    result = _decide([UNIT_REFERRER, writer])

    assert result.ok is True
    assert result.findings == ()
    assert result.resources[0].writers == ("writer:x",)
    assert result.resources[0].writers_without_lock == ()


def test_a_write_marker_on_a_comment_line_is_not_a_writer() -> None:
    writer = ModelReferrer(
        id="writer:x", kind="source", text="# uv pip install --python x"
    )

    result = _decide([UNIT_REFERRER, writer])

    assert result.ok is True
    assert result.resources[0].writers == ()
    assert result.resources[0].writers_without_lock == ()


# ------------------------------------------------------------------------------------------------------ pure
def test_decide_one_copy_is_deterministic_in_referrer_order() -> None:
    forward = _decide([UNIT_REFERRER, TICK_REFERRER])
    backward = _decide([TICK_REFERRER, UNIT_REFERRER])

    assert forward == backward
    assert [finding.code for finding in forward.findings] == [
        finding.code for finding in backward.findings
    ]


# ------------------------------------------------------------------------------------------------------ golden
def test_golden_chain_a_clean_read_emits_start_and_success_and_the_fold_counts_golden() -> (
    None
):
    sink = InMemoryEmissionSink()

    quiet_tick = ModelReferrer(
        id="tick:throughput", kind="tick", text='print("throughput")'
    )
    result = _handler(InMemoryReferrerReader([UNIT_REFERRER, quiet_tick]), sink).handle(
        ModelOneCopyCheckRequest(as_of=AS_OF)
    )

    assert result.ok is True
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert len({e.correlation_id for e in sink.events}) == 1
    assert {e.node for e in sink.events} == {NODE_NAME}
    assert {e.piece for e in sink.events} == {PIECE}
    assert {e.backend for e in sink.events} == {"in_memory"}

    receipts = _fold(sink)
    assert receipts.findings == ()
    (chain,) = receipts.chains
    assert chain.kind == "golden"
    (row,) = receipts.pieces
    assert row.golden_at is not None
    assert row.error_at is None


# ------------------------------------------------------------------------------------------------------- error
def test_error_chain_a_violation_is_returned_and_fails_the_chain_not_the_caller() -> (
    None
):
    sink = InMemoryEmissionSink()

    result = _handler(
        InMemoryReferrerReader([UNIT_REFERRER, TICK_REFERRER]), sink
    ).handle(ModelOneCopyCheckRequest(as_of=AS_OF))

    assert result.ok is False
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[1].failure_code == FAILURE_VIOLATION
    receipts = _fold(sink)
    (chain,) = receipts.chains
    assert chain.kind == "error"

    # Control: the corrected read of the multiple_live test is golden.
    clean = InMemoryEmissionSink()
    quiet_tick = ModelReferrer(
        id="tick:throughput", kind="tick", text='print("throughput")'
    )
    _handler(InMemoryReferrerReader([UNIT_REFERRER, quiet_tick]), clean).handle(
        ModelOneCopyCheckRequest(as_of=AS_OF)
    )
    assert [c.kind for c in _fold(clean).chains] == ["golden"]


@pytest.mark.parametrize("injected", list(EnumReferrerBreak))
def test_error_chain_a_read_that_fails_or_returns_nothing_is_a_typed_failure(
    injected: EnumReferrerBreak,
) -> None:
    sink = InMemoryEmissionSink()

    with pytest.raises(OneCopyCheckError) as raised:
        _handler(InMemoryReferrerReader([UNIT_REFERRER], injected), sink).handle(
            ModelOneCopyCheckRequest(as_of=AS_OF)
        )

    expected_code = {
        EnumReferrerBreak.REFERRERS_UNREADABLE: FAILURE_UNREADABLE,
        EnumReferrerBreak.REFERRERS_SILENT: FAILURE_SILENT,
    }[injected]
    assert raised.value.code == expected_code
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[1].failure_code == expected_code
    (chain,) = _fold(sink).chains
    assert chain.kind == "error"

    # Control: the same read without the injected break is golden.
    clean = InMemoryEmissionSink()
    _handler(InMemoryReferrerReader([UNIT_REFERRER]), clean).handle(
        ModelOneCopyCheckRequest(as_of=AS_OF)
    )
    assert [c.kind for c in _fold(clean).chains] == ["golden"]


# ----------------------------------------------------------------------------------------------------- overlay
def test_overlay_a_missing_overlay_refuses_naming_the_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))

    with pytest.raises(NodeOverlayNotFoundError, match=NODE_NAME):
        load_overlay()


def test_overlay_the_installed_overlay_loads_with_its_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(monkeypatch, tmp_path, NODE_NAME, EXAMPLE_OVERLAY_FIELDS)

    overlay = load_overlay()

    assert overlay.emission_piece == 10
    assert overlay.behaviours[0].fix_landed_in == "market_copy"
    assert overlay.resources[0].lock_markers == ("staging-lock-acquire",)


def test_overlay_a_fix_landed_in_that_is_not_an_implementation_id_refuses() -> None:
    fields = {
        **OVERLAY_FIELDS,
        "behaviours": [
            {
                "name": "lab_fill",
                "fix_landed_in": "ghost_copy",
                "implementations": [
                    {"id": "tick_copy", "markers": ["pkg_a.nodes.node_fill"]},
                ],
            }
        ],
    }

    with pytest.raises(ValidationError, match="fix_landed_in"):
        ModelOneCopyCheckOverlay.model_validate(fields)
