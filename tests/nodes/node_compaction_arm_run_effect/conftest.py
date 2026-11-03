# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Small replay files and typed, observable compactor and premise scripts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path

import pytest

from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
    EnumArm,
    EnumReplySource,
    ModelAdapterOutput,
    ModelArmCase,
    ModelArmRunRequest,
    ModelFork,
)

DEFAULT_ARMS = (EnumArm.STOCK_RERUN, EnumArm.MOD, EnumArm.STOCK_RECORDED)


def digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


class ScriptedPremises:
    def __init__(
        self,
        manual: bool | None = True,
        keeps_transcript: bool | None = True,
    ) -> None:
        self.manual = manual
        self.keeps_transcript = keeps_transcript
        self.manual_calls = 0
        self.transcript_calls = 0

    def manual_compaction_accepted(self, fork: ModelFork) -> bool | None:
        self.manual_calls += 1
        return self.manual

    def fork_keeps_transcript(self, fork: ModelFork) -> bool | None:
        self.transcript_calls += 1
        return self.keeps_transcript


class ScriptedAdapter:
    def __init__(
        self,
        output: ModelAdapterOutput | None,
        *,
        by_case: Mapping[str, ModelAdapterOutput | None] | None = None,
        effect: Callable[[ModelFork], None] | None = None,
    ) -> None:
        self.output = output
        self.by_case = dict(by_case or {})
        self.effect = effect
        self.calls: list[ModelFork] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def compact(self, fork: ModelFork) -> ModelAdapterOutput | None:
        self.calls.append(fork)
        if self.effect is not None:
            self.effect(fork)
        return self.by_case.get(fork.case_id, self.output)


def make_request(
    root: Path,
    *,
    arms: tuple[EnumArm, ...] = DEFAULT_ARMS,
    repeat_index: int = 0,
) -> ModelArmRunRequest:
    replay_root = root / "replay"
    cases: list[ModelArmCase] = []
    for case_id in ("case-a", "case-b"):
        directory = replay_root / case_id
        directory.mkdir(parents=True, exist_ok=True)
        prefix = directory / "prefix.jsonl"
        ledger = directory / "ledger.jsonl"
        prefix.write_bytes(f'{{"case":"{case_id}","text":"frozen prefix"}}\n'.encode())
        ledger.write_bytes(f'{{"case":"{case_id}","ruling":"ship"}}\n'.encode())
        cases.append(
            ModelArmCase(
                case_id=case_id,
                prefix_path=str(prefix),
                ledger_path=str(ledger),
                recorded_summary=f"Recorded native summary for {case_id}.",
                oracle_context=f"Oracle ruling for {case_id}: ship.",
                inverted_context=f"Inverted ruling for {case_id}: hold.",
            )
        )
    return ModelArmRunRequest(
        correlation_id="compaction-arm-run-tests",
        replay_root=str(replay_root),
        run_root=str(root / "run"),
        repeat_index=repeat_index,
        arms=arms,
        cases=tuple(cases),
    )


@pytest.fixture
def arm_request(tmp_path: Path) -> ModelArmRunRequest:
    return make_request(tmp_path)


@pytest.fixture
def premises() -> ScriptedPremises:
    return ScriptedPremises()


@pytest.fixture
def stock_adapter() -> ScriptedAdapter:
    return ScriptedAdapter(
        ModelAdapterOutput(
            compacted_context="Fresh native summary.", input_tokens=17, output_tokens=5
        )
    )


@pytest.fixture
def mod_adapter() -> ScriptedAdapter:
    return ScriptedAdapter(
        ModelAdapterOutput(
            compacted_context="Pinned mod pack.",
            input_tokens=23,
            output_tokens=7,
            reply_source=EnumReplySource.MOD,
        )
    )
