# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Loads every overlay document through the model class in this tree (OMN-23963).

The judgement of one document is the drift node's own (``node_overlay_drift_effect``: ``parse_pins`` reads the pins file and
``validate_entry`` runs a document through its model, naming the refused fields and never a value). That node judges the pinned
overlays against the models a RUNTIME has installed, once a day; this node judges them against the models of the tree under
review, so a change that makes a key required is refused before it merges. One implementation, two callers.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    find_node_overlay,
)
from omnimarket.nodes.node_overlay_drift_effect.adapters.overlay_home import (
    parse_pins,
)
from omnimarket.nodes.node_overlay_drift_effect.protocols import OverlayHomeError
from omnimarket.nodes.node_overlay_drift_effect.validate_program import (
    validate_entry,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.model_resolver import (
    ModelImportFailedError,
    ModelUnresolvedError,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.models import (
    EnumOverlayLoadFindingCode,
    ModelOverlayLoadFinding,
    ModelOverlayMainLoadCommand,
    ModelOverlayMainLoadResult,
)


class HandlerOverlayMainLoadCheck:
    """Loads every overlay document through the model class in this tree."""

    def __init__(
        self,
        *,
        resolve: Callable[[str], str],
        floor_check: Callable[[Path], Sequence[str]],
    ) -> None:
        """``resolve`` maps a pin's ``module:class`` to the spec that imports in this tree (a moved model's new home)."""
        self._resolve = resolve
        self._floor_check = floor_check

    def handle(
        self, command: ModelOverlayMainLoadCommand
    ) -> ModelOverlayMainLoadResult:
        environ = {OVERLAY_ROOTS_ENV: command.overlays_root}
        loaded: list[str] = []
        findings: list[ModelOverlayLoadFinding] = []

        def found(node: str, code: EnumOverlayLoadFindingCode, detail: str) -> None:
            findings.append(
                ModelOverlayLoadFinding(node=node, code=code, detail=detail)
            )

        try:
            pins = parse_pins(Path(command.pins_file).read_text(encoding="utf-8"))
        except OverlayHomeError as exc:
            found("pins", EnumOverlayLoadFindingCode.PIN_MALFORMED, str(exc))
            return ModelOverlayMainLoadResult(
                ok=False, loaded=(), findings=tuple(findings)
            )
        for pin in pins:
            node = pin.overlay_id
            path = find_node_overlay(node, environ)
            if path is None:
                found(
                    node,
                    EnumOverlayLoadFindingCode.DOCUMENT_ABSENT,
                    f"no <root>/{node}/overlay.yaml",
                )
                continue
            try:
                spec = self._resolve(pin.model)
            except ModelUnresolvedError:
                found(
                    node,
                    EnumOverlayLoadFindingCode.MODEL_ABSENT,
                    "the model class named by the pin exists nowhere in this tree, so no code reads the document",
                )
                continue
            except ModelImportFailedError as exc:
                found(node, EnumOverlayLoadFindingCode.MODEL_IMPORT_FAILED, str(exc))
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                found(
                    node,
                    EnumOverlayLoadFindingCode.DOCUMENT_INVALID,
                    f"document unreadable ({type(exc).__name__})",
                )
                continue
            verdict = validate_entry({"overlay_id": node, "model": spec, "text": text})
            match verdict["verdict"]:
                case "accepted":
                    loaded.append(node)
                case "rejected":
                    found(
                        node,
                        EnumOverlayLoadFindingCode.DOCUMENT_INVALID,
                        f"{spec.partition(':')[2]} refused the document; fields: {', '.join(verdict['fields'])}",
                    )
                case "unreadable":
                    found(
                        node,
                        EnumOverlayLoadFindingCode.DOCUMENT_INVALID,
                        f"document is not YAML ({verdict['error_class']})",
                    )
                case _:
                    found(
                        node,
                        EnumOverlayLoadFindingCode.MODEL_IMPORT_FAILED,
                        f"{spec} did not import ({verdict['error_class']})",
                    )
        try:
            for line in self._floor_check(Path(command.overlays_root)):
                if line.startswith("ok "):
                    loaded.append(line[3:])
        except (
            Exception
        ) as exc:  # the floor check raises whatever its overlay loader raises
            found(
                "floor_overlays",
                EnumOverlayLoadFindingCode.DOCUMENT_INVALID,
                f"{type(exc).__name__}: {exc}",
            )
        return ModelOverlayMainLoadResult(
            ok=not any(f.refuses for f in findings),
            loaded=tuple(loaded),
            findings=tuple(findings),
        )
