# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor overlay accepts the ``ledger_projection`` block the overlays repository's main branch carries (OMN-25348).

The overlays repository's main branch holds ``ledger_projection`` in the floor document. The overlay-main-load check judges that
document through this tree's own ``ModelFactoryFloorOverlay``, whose ``extra="forbid"`` refuses a key the model lacks, so a tree whose
model has no such key reads the check red on every pull request.

Three things are pinned here: the block exactly as overlays main carries it loads through the check's own loader
(``node_overlay_main_load_check_effect``), a block that differs by one key does not, and the floor effect accepts the block without
acting on it (the reader that acts on it is not in this tree).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorOverlay,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.handler_overlay_main_load_check import (
    HandlerOverlayMainLoadCheck,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.model_resolver import (
    default_search_roots,
    resolve_spec,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.models import (
    ModelOverlayMainLoadCommand,
)

from .test_ledger_source_named import _named, _run_effect
from .test_line_board_facts import OVERLAY
from .test_runtime_factory_floor import world

pytestmark = pytest.mark.unit

__all__ = ["world"]

# Pasted verbatim from overlays/node_factory_floor_effect/overlay.yaml (lines 220 to 224) at the overlays repository's main commit
# a951d8554d2f6dd3d78db46b04cc1184fef5c8bf ("OMN-24490: The factory floor reads its ledger window from the projection (overlay)", #542).
OVERLAYS_MAIN_BLOCK_YAML = """\
ledger_projection:
  reader: factory-floor
  cache_ttl_seconds: 21600
  read_workers: 4
  settle_seconds: 3600
"""
PROJECTION_BLOCK: dict[str, Any] = yaml.safe_load(OVERLAYS_MAIN_BLOCK_YAML)[
    "ledger_projection"
]

FLOOR_MODEL_SPEC = (
    "omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime"
    ":ModelFactoryFloorOverlay"
)
# The floor document's line of the overlays repository's .omnimarket-node-pins at the same commit, verbatim.
FLOOR_PIN_LINE = f"node_factory_floor_effect 3b8c55d1277fe1963c78da3c041038f5452f4b3a {FLOOR_MODEL_SPEC}"


def _document(**extra: Any) -> dict[str, Any]:
    return {
        "plan_path_contains": ["x"],
        "processes": [],
        "roles": [],
        "release_required_repos": [],
        "held_gates": [],
        **extra,
    }


def _check_the_floor_document(tmp_path: Path, block_yaml: str) -> Any:
    """Drive a floor document through the overlay-main-load check's own loader, the way its pins file names the floor model."""
    root = tmp_path / "overlays"
    directory = root / "node_factory_floor_effect"
    directory.mkdir(parents=True)
    (directory / "overlay.yaml").write_text(
        yaml.safe_dump(OVERLAY, sort_keys=True) + block_yaml, encoding="utf-8"
    )
    pins = tmp_path / "pins.txt"
    pins.write_text(f"# pins\n{FLOOR_PIN_LINE}\n", encoding="utf-8")
    handler = HandlerOverlayMainLoadCheck(
        resolve=lambda spec: resolve_spec(spec, default_search_roots()),
        # The production floor_check (check_overlays) loads only the floor-simple, lab-state and alarm overlays, never the
        # node_factory_floor_effect document under test; this root holds none of the three, so the stub leaves out an unrelated refusal.
        floor_check=lambda _root: [],
    )
    return handler.handle(
        ModelOverlayMainLoadCommand(overlays_root=str(root), pins_file=str(pins))
    )


def test_the_overlay_main_block_loads_through_the_floor_model() -> None:
    overlay = ModelFactoryFloorOverlay.model_validate(
        _document(ledger_projection=PROJECTION_BLOCK)
    )
    assert overlay.ledger_projection is not None
    assert overlay.ledger_projection.model_dump() == PROJECTION_BLOCK


def test_the_overlay_main_block_loads_through_the_overlay_main_load_check(
    tmp_path: Path,
) -> None:
    """The check's own loader accepts the floor document carrying the block exactly as overlays main holds it."""
    result = _check_the_floor_document(tmp_path, OVERLAYS_MAIN_BLOCK_YAML)
    assert result.findings == ()
    assert result.ok
    assert result.loaded == ("node_factory_floor_effect",)


@pytest.mark.parametrize(
    ("changed", "refused_field"),
    [
        (
            OVERLAYS_MAIN_BLOCK_YAML.replace("read_workers", "read_workerz"),
            "ledger_projection.read_workerz",
        ),
        (
            OVERLAYS_MAIN_BLOCK_YAML.replace(
                "reader: factory-floor", "reader: Not A Name"
            ),
            "ledger_projection.reader",
        ),
        (
            OVERLAYS_MAIN_BLOCK_YAML.replace(
                "cache_ttl_seconds: 21600", "cache_ttl_seconds: 0"
            ),
            "ledger_projection.cache_ttl_seconds",
        ),
        (
            OVERLAYS_MAIN_BLOCK_YAML + "  extra_key: 1\n",
            "ledger_projection.extra_key",
        ),
    ],
    ids=["renamed-key", "bad-reader", "zero-ttl", "added-key"],
)
def test_error_a_block_that_differs_from_overlays_main_is_refused_by_the_check(
    tmp_path: Path, changed: str, refused_field: str
) -> None:
    """The failing control: one key of the overlays main block changed, and the check refuses the floor document at that field."""
    assert changed != OVERLAYS_MAIN_BLOCK_YAML
    result = _check_the_floor_document(tmp_path, changed)
    assert not result.ok
    assert result.loaded == ()
    (finding,) = result.findings
    assert finding.node == "node_factory_floor_effect"
    assert (
        finding.detail
        == f"ModelFactoryFloorOverlay refused the document; fields: {refused_field}"
    )


def test_an_overlay_without_the_block_still_loads() -> None:
    overlay = ModelFactoryFloorOverlay.model_validate(_document())
    assert overlay.ledger_projection is None


def test_the_floor_effect_accepts_the_ledger_projection_block_and_still_reads_the_file(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block is accepted and not yet acted on (OMN-19998): the window is still the ledger file read the overlay's ``ledger_source`` names.

    The reader that answers a window from the projection is not in this tree. Choosing the other way (a run refuses while the block is
    set) turns this test red, as does a floor effect that reads the block and skips the file.
    """
    argvs, record = _run_effect(
        world,
        monkeypatch,
        {**OVERLAY, "ledger_source": "file", "ledger_projection": PROJECTION_BLOCK},
    )
    assert [_named(argv) for argv in argvs] == ["file"]
    health = record["sources"]["health"]
    ledger = next(entry for entry in health if entry["source"] == "ledger")
    assert ledger["state"] == "read", ledger


@pytest.mark.parametrize(
    "at_bound",
    [
        {**PROJECTION_BLOCK, "read_workers": 1},
        {**PROJECTION_BLOCK, "settle_seconds": 0},
        {**PROJECTION_BLOCK, "cache_ttl_seconds": 1},
        {k: v for k, v in PROJECTION_BLOCK.items() if k != "read_workers"},
    ],
    ids=[
        "read-workers-one",
        "settle-seconds-zero",
        "cache-ttl-seconds-one",
        "read-workers-absent",
    ],
)
def test_a_block_at_each_bound_is_accepted(at_bound: dict[str, Any]) -> None:
    """The other side of each bound the error chain below pins: the smallest accepted value, and read_workers left out."""
    overlay = ModelFactoryFloorOverlay.model_validate(
        _document(ledger_projection=at_bound)
    )
    assert overlay.ledger_projection is not None
    assert overlay.ledger_projection.model_dump() == {"read_workers": None, **at_bound}


@pytest.mark.parametrize(
    ("broken", "loc", "error_type"),
    [
        (
            {**PROJECTION_BLOCK, "unknown_key": 1},
            ("ledger_projection", "unknown_key"),
            "extra_forbidden",
        ),
        (
            {**PROJECTION_BLOCK, "cache_ttl_seconds": 0},
            ("ledger_projection", "cache_ttl_seconds"),
            "greater_than_equal",
        ),
        (
            {**PROJECTION_BLOCK, "reader": "Not A Reader"},
            ("ledger_projection", "reader"),
            "string_pattern_mismatch",
        ),
        (
            {k: v for k, v in PROJECTION_BLOCK.items() if k != "settle_seconds"},
            ("ledger_projection", "settle_seconds"),
            "missing",
        ),
        (
            {**PROJECTION_BLOCK, "read_workers": 0},
            ("ledger_projection", "read_workers"),
            "greater_than_equal",
        ),
        (
            {**PROJECTION_BLOCK, "settle_seconds": -1},
            ("ledger_projection", "settle_seconds"),
            "greater_than_equal",
        ),
        (
            {k: v for k, v in PROJECTION_BLOCK.items() if k != "reader"},
            ("ledger_projection", "reader"),
            "missing",
        ),
        (
            {k: v for k, v in PROJECTION_BLOCK.items() if k != "cache_ttl_seconds"},
            ("ledger_projection", "cache_ttl_seconds"),
            "missing",
        ),
    ],
    ids=[
        "unknown-key",
        "zero-ttl",
        "bad-reader-name",
        "missing-settle-seconds",
        "zero-read-workers",
        "negative-settle-seconds",
        "missing-reader",
        "missing-cache-ttl-seconds",
    ],
)
def test_error_chain_a_malformed_block_is_refused_at_the_field_and_for_the_reason(
    broken: dict[str, Any], loc: tuple[str, ...], error_type: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        ModelFactoryFloorOverlay.model_validate(_document(ledger_projection=broken))
    errors = [(err["loc"], err["type"]) for err in caught.value.errors()]
    assert errors == [(loc, error_type)]
