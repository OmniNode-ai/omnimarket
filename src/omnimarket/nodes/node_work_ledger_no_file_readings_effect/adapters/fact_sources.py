# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probe backends. The command backend runs the argv each reading's overlay variable holds and
parses its stdout as the reading's fact model; nothing about a host or a path is in this file."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable, Mapping

from pydantic import BaseModel

from omnimarket.nodes.node_work_ledger_no_file_readings_effect.models import (
    ModelAbsenceFacts,
    ModelCensusFacts,
    ModelFileProbeFacts,
    ModelNoFileFacts,
    ModelNoFileRoute,
    ModelPublishFacts,
    ModelRebuildFacts,
    ModelReceiptFacts,
    ModelRestoreFacts,
)

Runner = Callable[[list[str], int], str]


class InMemoryNoFileFacts:
    """The facts a test or a fixture world holds."""

    def __init__(self, facts: ModelNoFileFacts) -> None:
        self._facts = facts

    def file_probe(self) -> ModelFileProbeFacts:
        return self._facts.file_probe

    def census(self) -> ModelCensusFacts:
        return self._facts.census

    def absence(self) -> ModelAbsenceFacts:
        return self._facts.absence

    def rebuild(self) -> ModelRebuildFacts:
        return self._facts.rebuild

    def restore(self) -> ModelRestoreFacts:
        return self._facts.restore

    def publish(self) -> ModelPublishFacts:
        return self._facts.publish

    def receipt(self) -> ModelReceiptFacts:
        return self._facts.receipt


def run_argv(argv: list[str], timeout_seconds: int) -> str:
    done = subprocess.run(
        argv, check=False, capture_output=True, text=True, timeout=timeout_seconds
    )
    if done.returncode != 0:
        raise OSError(f"probe exited {done.returncode}")
    return done.stdout


class CommandNoFileFacts:
    """Each reading's facts from a command named by an environment variable the overlay lists."""

    def __init__(
        self,
        route: ModelNoFileRoute,
        environ: Mapping[str, str],
        runner: Runner = run_argv,
    ) -> None:
        self._route = route
        self._environ = environ
        self._runner = runner

    def _get[M: BaseModel](self, reading: str, model: type[M]) -> M:
        variable = self._route.command_envs.get(reading, "")
        words = self._environ.get(variable, "").strip() if variable else ""
        if not words:
            raise OSError(f"probe-not-wired-{reading}")
        try:
            text = self._runner(shlex.split(words), self._route.timeout_seconds)
            return model.model_validate(json.loads(text))
        except subprocess.TimeoutExpired as exc:
            raise OSError("probe-timed-out") from exc

    def file_probe(self) -> ModelFileProbeFacts:
        return self._get("file_probe", ModelFileProbeFacts)

    def census(self) -> ModelCensusFacts:
        return self._get("census", ModelCensusFacts)

    def absence(self) -> ModelAbsenceFacts:
        return self._get("absence", ModelAbsenceFacts)

    def rebuild(self) -> ModelRebuildFacts:
        return self._get("rebuild", ModelRebuildFacts)

    def restore(self) -> ModelRestoreFacts:
        return self._get("restore", ModelRestoreFacts)

    def publish(self) -> ModelPublishFacts:
        return self._get("publish", ModelPublishFacts)

    def receipt(self) -> ModelReceiptFacts:
        return self._get("receipt", ModelReceiptFacts)
