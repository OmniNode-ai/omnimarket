# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24556: the register read's journal write failures ride to the tick decision.

The facts step's register source returns the judged register *and* the read's ``sink_errors``
(``<row_id>: <exception name>`` per row whose read event the journal did not take). The handler
carries them on ``ruling_register_sink_errors`` only when there are any, so a facts result whose
journal took every read reads exactly as before, and the default source passes the pair through
unchanged from ``read_register``.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.models.model_ruling_register import (
    EnumRulingVerdict,
    ModelRulingRegisterResult,
    ModelRulingVerdict,
)
from omnimarket.nodes.node_throughput_tick_facts_effect.handlers.handler_throughput_tick_facts import (
    HandlerThroughputTickFacts,
)
from omnimarket.nodes.node_throughput_tick_facts_effect.handlers.tick_facts_ports import (
    RulingRegisterSource,
    ScriptedTickFactsSource,
)
from omnimarket.nodes.node_throughput_tick_facts_effect.models.model_throughput_tick_facts import (
    ModelThroughputTickFactsRequest,
)

pytestmark = pytest.mark.unit

NOW = "2026-11-02T15:02:01Z"


def judged() -> ModelRulingRegisterResult:
    """A judged register holding one TRUE verdict; what varies here is its journal."""
    return ModelRulingRegisterResult(
        correlation_id="corr-ruling-register-sink-errors",
        passed=True,
        verdicts=(
            ModelRulingVerdict(
                row_id="row-quiet",
                ruling=NOW,
                owner="example-lane",
                verdict=EnumRulingVerdict.TRUE,
                detail="",
                why="",
                fix="",
            ),
        ),
        true_count=1,
        false_count=0,
        unread_count=0,
    )


class StubRegisterSource:
    """The handler's register port, answered from one fixed pair."""

    def __init__(
        self,
        result: ModelRulingRegisterResult,
        sink_errors: tuple[str, ...] = (),
    ) -> None:
        self._result = result
        self._sink_errors = sink_errors

    def read(
        self, overlay_roots: str
    ) -> tuple[ModelRulingRegisterResult, tuple[str, ...]]:
        return self._result, self._sink_errors


def facts(sink_errors: tuple[str, ...]) -> Any:
    return (
        HandlerThroughputTickFacts(
            ScriptedTickFactsSource(),
            ruling_register=StubRegisterSource(judged(), sink_errors),
        )
        .handle(
            ModelThroughputTickFactsRequest(
                now=NOW, landing_state_dir="/l", state_dir="/s", open_basis="b"
            )
        )
        .facts
    )


def test_journal_write_failures_ride_on_ruling_register_sink_errors() -> None:
    read = facts(("row-a: OSError",))
    assert read.ruling_register is not None
    assert read.ruling_register_sink_errors == ["row-a: OSError"]
    assert read.ruling_register_unread is None
    dump = read.model_dump(exclude_unset=True)
    assert dump["ruling_register_sink_errors"] == ["row-a: OSError"]
    assert "ruling_register_unread" not in dump


def test_a_journal_that_took_every_read_leaves_the_field_unset() -> None:
    read = facts(())
    assert read.ruling_register is not None
    assert "ruling_register_sink_errors" not in read.model_dump(exclude_unset=True)


def test_the_default_source_returns_the_read_pair_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read = judged()
    seen: dict[str, Any] = {}

    def fake_read_register(environ: dict[str, str], **_kwargs: Any) -> Any:
        seen.update(environ)
        return read, ("row-a: OSError",)

    monkeypatch.setattr(
        "omnimarket.nodes.node_throughput_tick_facts_effect.handlers"
        ".tick_facts_ports.read_register",
        fake_read_register,
    )
    source = RulingRegisterSource()
    assert source.read("roots-a:roots-b") == (read, ("row-a: OSError",))
    assert seen[OVERLAY_ROOTS_ENV] == "roots-a:roots-b"
