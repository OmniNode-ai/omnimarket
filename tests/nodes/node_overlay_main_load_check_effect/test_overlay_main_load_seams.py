# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests for the overlay main load check node (OMN-23963): the two sides each test ties together."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    check_overlays,
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
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    _overlay,
)

pytestmark = pytest.mark.unit


def _write_root(tmp_path: Path, documents: dict[str, dict]) -> Path:
    root = tmp_path / "overlays"
    for node, document in documents.items():
        directory = root / node
        directory.mkdir(parents=True)
        (directory / "overlay.yaml").write_text(
            yaml.safe_dump(document, sort_keys=True), encoding="utf-8"
        )
    return root


def _write_pins(tmp_path: Path, lines: list[str]) -> Path:
    pins = tmp_path / "pins.txt"
    pins.write_text("# pins\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return pins


def _handler(
    models: dict[str, type[BaseModel]] | None = None,
    floor: object = None,
) -> HandlerOverlayMainLoadCheck:
    from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.model_resolver import (
        ModelUnresolvedError,
    )

    def resolve(spec: str) -> str:
        if models is not None:
            try:
                return f"{models[spec].__module__}:{models[spec].__name__}"
            except KeyError:
                raise ModelUnresolvedError(f"{spec} not in synthetic models") from None
        return resolve_spec(spec, default_search_roots())

    return HandlerOverlayMainLoadCheck(
        resolve=resolve, floor_check=floor or (lambda _root: [])
    )


def test_seam_pins_file_format_matches_overlays_repository_layout(
    tmp_path: Path,
) -> None:
    """The handler reads the pins-file line format exactly as the overlays repository writes it."""
    document = _overlay(tmp_path).model_dump(mode="json")
    root = _write_root(
        tmp_path,
        {
            "node_factory_floor_publish_effect": document,
            "node_overlay_main_load_check_effect": {
                "overlays_root": "x",
                "pins_file": "y",
            },
        },
    )
    pins = _write_pins(
        tmp_path,
        [
            "node_factory_floor_publish_effect abc123 omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish:ModelFactoryFloorPublishOverlay",
            "node_overlay_main_load_check_effect def456 omnimarket.nodes.node_overlay_main_load_check_effect.models.model_overlay_main_load_check:ModelOverlayMainLoadCommand",
        ],
    )
    result = _handler().handle(
        ModelOverlayMainLoadCommand(overlays_root=str(root), pins_file=str(pins))
    )
    assert result.ok
    assert result.loaded == (
        "node_factory_floor_publish_effect",
        "node_overlay_main_load_check_effect",
    )


def test_seam_floor_check_loads_the_fixture_overlays(tmp_path: Path) -> None:
    """The real floor check from node_floor_simple_effect loads the fixture overlays."""
    fixture_root = (
        Path(__file__).resolve().parents[1]
        / "node_floor_simple_effect/fixtures/overlays"
    )
    pins = _write_pins(tmp_path, [])
    result = _handler(floor=check_overlays).handle(
        ModelOverlayMainLoadCommand(
            overlays_root=str(fixture_root), pins_file=str(pins)
        )
    )
    assert result.ok
    assert len(result.loaded) >= 3


def test_seam_floor_check_refuses_when_an_overlay_is_truncated(
    tmp_path: Path,
) -> None:
    """The floor check refuses when an overlay document is truncated to an empty mapping."""
    fixture_root = (
        Path(__file__).resolve().parents[1]
        / "node_floor_simple_effect/fixtures/overlays"
    )
    copied = tmp_path / "overlays"
    shutil.copytree(fixture_root, copied)
    for overlay in copied.rglob("overlay.yaml"):
        overlay.write_text("{}", encoding="utf-8")
    pins = _write_pins(tmp_path, [])
    result = _handler(floor=check_overlays).handle(
        ModelOverlayMainLoadCommand(overlays_root=str(copied), pins_file=str(pins))
    )
    assert not result.ok
    assert any(f.node == "floor_overlays" for f in result.findings)


def test_seam_contract_topics_and_terminal_event() -> None:
    """The contract declares the three topics and its terminal event is one of them."""
    import omnimarket.nodes.node_overlay_main_load_check_effect as _pkg

    contract = yaml.safe_load(
        (Path(_pkg.__file__).parent / "contract.yaml").read_text(encoding="utf-8")
    )
    topics = contract["topics"]
    terminal = contract["terminal_event"]
    assert set(topics) == {
        "overlay_main_load_check_requested",
        "overlay_main_load_check_completed",
        "overlay_main_load_check_failed",
    }
    assert terminal in topics.values()


@pytest.mark.parametrize("drop_limits", [False, True])
def test_seam_verdict_agrees_with_the_drift_node_validator(
    tmp_path: Path, drop_limits: bool
) -> None:
    """This node and node_overlay_drift_effect judge one document alike: both read the same validate_entry verdict."""
    from omnimarket.nodes.node_overlay_drift_effect.validate_program import (
        validate_entry,
    )

    spec = "omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish:ModelFactoryFloorPublishOverlay"
    document = _overlay(tmp_path).model_dump(mode="json")
    if drop_limits:
        document.pop("limits")
    drift = validate_entry(
        {"overlay_id": "node_x", "model": spec, "text": yaml.safe_dump(document)}
    )
    root = _write_root(tmp_path, {"node_x": document})
    pins = _write_pins(tmp_path, [f"node_x abc123 {spec}"])
    result = _handler().handle(
        ModelOverlayMainLoadCommand(overlays_root=str(root), pins_file=str(pins))
    )
    assert (drift["verdict"] == "accepted") is result.ok
    if drop_limits:
        assert drift["verdict"] == "rejected"
        assert "limits" in drift["fields"]
        assert "limits" in result.findings[0].detail
