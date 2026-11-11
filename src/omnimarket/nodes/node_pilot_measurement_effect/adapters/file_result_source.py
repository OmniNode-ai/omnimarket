# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The file result source: each pilot's measured result from the JSON file its variable names."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_pilot_measurement_effect.models.model_pilot_measurement import (
    ModelPilotResultRead,
)
from omnimarket.nodes.node_pilot_measurement_effect.overlay import ModelPilotDeclaration
from omnimarket.nodes.node_pilot_measurement_effect.protocols import PilotResultError


class FileResultSource:
    """Reads the JSON result file the declaration's ``result_file_env`` names.

    The file holds ``{"measured_at": "<ISO time with zone>", "metrics": {"<metric>":
    <number>}}``. Every failure is typed: a variable that is not set, a file that is missing or
    unreadable, and a document that is not a typed result all raise ``PilotResultError``.
    """

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ

    def read(self, declaration: ModelPilotDeclaration) -> ModelPilotResultRead:
        path_env = declaration.result_file_env
        path_text = self._environ.get(path_env, "").strip()
        if not path_text:
            raise PilotResultError(f"{path_env}-not-set")
        try:
            document = json.loads(Path(path_text).read_text(encoding="utf-8"))
            return ModelPilotResultRead.model_validate(document)
        except OSError as exc:
            raise PilotResultError(
                f"result-file-unreadable:{type(exc).__name__}"
            ) from None
        except (ValueError, ValidationError) as exc:
            raise PilotResultError(f"result-not-typed:{type(exc).__name__}") from None
