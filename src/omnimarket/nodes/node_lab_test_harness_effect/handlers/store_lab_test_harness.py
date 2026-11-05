# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stores of the current reading: in memory (the default under test) and a JSON file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from string import Template

from pydantic import ValidationError

from omnimarket.models.lab_test_harness import ModelLabTestHarnessReading


def resolve_result_path(raw: str, environ: Mapping[str, str] | None = None) -> Path:
    """The overlay's result path with each \\${NAME} read from the environment.

    A name the environment does not hold refuses: a literal \\${NAME} directory would
    otherwise be created and the floor would read an empty place.
    """
    env = os.environ if environ is None else environ
    try:
        return Path(Template(raw).substitute(env))
    except (KeyError, ValueError) as exc:
        raise ValueError(
            f"current_result_path names {exc.args[0]!r}, which the environment does not supply"
        ) from None


class InMemoryLabTestHarnessStore:
    """Holds the reading in the process."""

    def __init__(self) -> None:
        self._reading: ModelLabTestHarnessReading | None = None

    def write(self, reading: ModelLabTestHarnessReading) -> None:
        self._reading = reading

    def read(self) -> ModelLabTestHarnessReading | None:
        return self._reading


class JsonFileLabTestHarnessStore:
    """One JSON document at a path the overlay names, replaced whole on each write."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, reading: ModelLabTestHarnessReading) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(reading.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self._path)

    def read(self) -> ModelLabTestHarnessReading | None:
        """The stored reading; None when no file exists. An unreadable file raises."""
        if not self._path.is_file():
            return None
        try:
            return ModelLabTestHarnessReading.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise ValueError(
                f"the stored harness reading at {self._path} is unreadable ({type(exc).__name__})"
            ) from None
