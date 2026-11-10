# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scripted model callers and case builders shared by the probe-run tests."""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any

import pytest

from omnimarket.nodes.node_compaction_probe_run_effect.models.model_probe_run import (
    EnumControl,
    ModelProbeCase,
    ModelProbeManifest,
    ModelProbeRunSettings,
)

ANSWERER = "lab-answerer-27b"
JUDGE = "claude-judge-5"
SETTINGS = ModelProbeRunSettings(temperature=0.0, max_tokens=256)
N_RULINGS = 14


def manifest() -> ModelProbeManifest:
    return ModelProbeManifest(
        candidate_id="cand-stock",
        node_commits=("ce1211922",),
        served_model_ids=(ANSWERER, JUDGE),
        prompt_hash="p" * 8,
        probe_set_hash="s" * 8,
        continuation_prompt_hash="c" * 8,
        caps_data_hash="k" * 8,
        recovery_list_hash="r" * 8,
        preregistration_hash="g" * 8,
    )


def quote(i: int) -> str:
    return f"Ship the zeta{i} lane behind the alpha{i} gate before friday"


def cases() -> tuple[ModelProbeCase, ...]:
    built: list[ModelProbeCase] = []
    for i in range(N_RULINGS):
        inverted = f"Ruling in force: Hold the zeta{i} lane, never ship it"
        for control, context in (
            (EnumControl.ORACLE, f"stamp{i} | RULING | {quote(i)}"),
            (EnumControl.EMPTY, ""),
            (EnumControl.INVERTED, inverted),
        ):
            built.append(
                ModelProbeCase(
                    case_id=f"{control.value}-{i}",
                    control=control,
                    stamp=f"2026-10-01T00:00:{i:02d}Z",
                    probe_text=f"What did the operator rule about lane {i}?",
                    context=context,
                    question=f"What did the operator rule about lane {i}?",
                    quote=quote(i),
                    predecessor_text=f"Hold the zeta{i} lane, never ship it",
                )
            )
    return tuple(built)


def scripted_caller(*, answerer_ok: bool = True, judge_wrong_on_oracle: int = 0) -> Any:
    """A caller that answers from the context and judges by lexical rule."""

    class Caller:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self._oracle_seen = 0

        def call(
            self, model_id: str, prompt: str, settings: ModelProbeRunSettings
        ) -> str | None:
            self.calls.append((model_id, prompt))
            if model_id == ANSWERER:
                if not answerer_ok:
                    return None
                if "Ship the zeta" in prompt:
                    return (
                        "The lane ships behind the gate: " + prompt.split("|")[-1][:60]
                    )
                if "never ship it" in prompt:
                    return "The lane is held and must never ship."
                return "I do not know."
            assert model_id == JUDGE
            answer = prompt.split("ANSWER:", 1)[1]
            if "Ship the zeta" in answer:
                self._oracle_seen += 1
                if self._oracle_seen <= judge_wrong_on_oracle:
                    return "CONTRADICTED"
                return "RECALLED"
            if "never ship" in answer:
                return "CONTRADICTED"
            return "ABSENT"

    return Caller()


CallerFactory = Callable[..., Any]


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    attempts: list[str] = []

    def refuse(*args: object, **kwargs: object) -> None:
        attempts.append("network")
        raise AssertionError("network call in a replay run")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    return attempts
