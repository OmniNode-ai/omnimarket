# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The crossing name labels are overlay values: packaged default, overlay, refusal."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.seam_crossing_name import (
    ModelSeamCrossing,
    ModelSeamCrossingNameRequest,
)
from omnimarket.models.seam_field_diff import ModelSeamFieldFacts, ModelSeamModelSummary
from omnimarket.nodes.node_seam_crossing_name_compute.handlers.handler_seam_crossing_name import (
    HandlerSeamCrossingName,
)
from omnimarket.nodes.node_seam_crossing_name_compute.overlay import (
    NODE_NAME,
    load_seam_crossing_naming,
)

pytestmark = pytest.mark.unit

ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"


def _crossing(body_keys: tuple[str, ...]) -> ModelSeamCrossing:
    facts = ModelSeamFieldFacts(
        type_name="string", required=True, nullable=False, has_default=False
    )
    return ModelSeamCrossing(
        crossing_id="c1",
        seam_id="s1",
        topic="t",
        consumer=ModelSeamModelSummary(
            model_ref="mod.C",
            fields={"a": facts},
            nested={},
            extra_forbidden=True,
            has_adapter=False,
        ),
        body_keys=body_keys,
    )


def _write_overlay(root: Path, text: str) -> None:
    node_dir = root / NODE_NAME
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(text)


def test_packaged_contract_labels_are_the_default() -> None:
    naming = load_seam_crossing_naming({})
    assert naming.missing_required == "missing_field"
    assert naming.undeclared_key == "unknown_field"
    assert naming.clean == "matches"
    verdict = HandlerSeamCrossingName().handle(
        ModelSeamCrossingNameRequest(crossings=(_crossing(("a", "zzz")),))
    )
    assert verdict.names[0].name == "unknown_field"


def test_an_overlay_replaces_the_labels_with_no_code_change(tmp_path: Path) -> None:
    _write_overlay(
        tmp_path,
        "missing_required: absent_required\nundeclared_key: extra_key\nclean: clean_body\n",
    )
    naming = load_seam_crossing_naming({ROOTS_ENV: str(tmp_path)})
    handler = HandlerSeamCrossingName()
    verdict = handler.handle(
        ModelSeamCrossingNameRequest(crossings=(_crossing(()),), naming=naming)
    )
    assert verdict.names[0].name == "absent_required"
    assert (
        handler.handle(
            ModelSeamCrossingNameRequest(
                crossings=(_crossing(("a", "zzz")),), naming=naming
            )
        )
        .names[0]
        .name
        == "extra_key"
    )
    assert (
        handler.handle(
            ModelSeamCrossingNameRequest(crossings=(_crossing(("a",)),), naming=naming)
        )
        .names[0]
        .name
        == "clean_body"
    )
    assert verdict.counts == {"absent_required": 1}


def test_an_invalid_overlay_refuses_and_never_falls_back(tmp_path: Path) -> None:
    _write_overlay(tmp_path, "missing_required: only_one_label\n")
    with pytest.raises(NodeOverlayError):
        load_seam_crossing_naming({ROOTS_ENV: str(tmp_path)})


def test_the_request_resolves_the_overlay_at_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlay is read when the request is built, never while naming."""
    _write_overlay(
        tmp_path,
        "missing_required: absent_required\nundeclared_key: extra_key\nclean: clean_body\n",
    )
    monkeypatch.setenv(ROOTS_ENV, str(tmp_path))
    request = ModelSeamCrossingNameRequest(crossings=(_crossing(()),))
    assert request.naming.missing_required == "absent_required"
    verdict = HandlerSeamCrossingName().handle(request)
    assert verdict.names[0].name == "absent_required"


def test_an_invalid_overlay_refuses_the_request_not_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_overlay(tmp_path, "missing_required: only_one_label\n")
    monkeypatch.setenv(ROOTS_ENV, str(tmp_path))
    # Purity: constructing the handler reads no environment and no overlay file.
    handler = HandlerSeamCrossingName()
    assert handler is not None
    with pytest.raises(NodeOverlayError):
        ModelSeamCrossingNameRequest(crossings=(_crossing(()),))


def test_the_handler_holds_no_constructor_state() -> None:
    assert HandlerSeamCrossingName.__init__ is object.__init__
