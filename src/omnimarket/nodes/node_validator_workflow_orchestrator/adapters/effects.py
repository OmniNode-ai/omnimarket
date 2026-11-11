# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract source, trace emit and gate effects, in memory and on files (OMN-23078)."""

from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_validator_workflow_orchestrator.models.model_validator_workflow import (
    ModelGateDecision,
    ModelTraceEvent,
)


class PackageContractSource:
    """Reads contracts shipped inside the installed packages (omnibase_core's validators)."""

    def node_contract(self, node_package: str) -> dict[str, Any]:
        text = (
            resources.files(node_package).joinpath("contract.yaml").read_text("utf-8")
        )
        return dict(yaml.safe_load(text))

    def validation_contract(self, package: str, file_name: str) -> dict[str, Any]:
        return dict(
            yaml.safe_load(
                resources.files(package).joinpath(file_name).read_text("utf-8")
            )
        )


class InMemoryTraceEmitter:
    def __init__(self) -> None:
        self.events: list[ModelTraceEvent] = []

    def emit(self, topic: str, payload: dict[str, object]) -> None:
        self.events.append(ModelTraceEvent(topic=topic, payload=payload))


class FileTraceEmitter:
    """Appends one JSON line per event to a spool file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def emit(self, topic: str, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"topic": topic, "payload": payload}, sort_keys=True)
            )
            handle.write("\n")

    @property
    def events(self) -> list[ModelTraceEvent]:
        if not self._path.exists():
            return []
        return [
            ModelTraceEvent.model_validate_json(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class InMemoryCheckGate:
    def __init__(self) -> None:
        self.decisions: list[ModelGateDecision] = []

    def post(self, decision: ModelGateDecision) -> None:
        self.decisions.append(decision)


class FileCheckGate:
    """Writes the latest decision per ``repo#pr@head`` into one JSON document."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        return dict(json.loads(self._path.read_text(encoding="utf-8")))

    def post(self, decision: ModelGateDecision) -> None:
        document = self._read()
        document[f"{decision.repo}#{decision.pr}@{decision.head}"] = (
            decision.model_dump(mode="json")
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)

    @property
    def decisions(self) -> list[ModelGateDecision]:
        return [ModelGateDecision.model_validate(v) for v in self._read().values()]
