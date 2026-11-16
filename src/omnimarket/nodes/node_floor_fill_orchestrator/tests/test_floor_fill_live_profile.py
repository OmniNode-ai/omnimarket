# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled tick binds the live profile: the live factory gets the overlay settings and the DSN.

The tests run ``run_floor_fill_tick`` directly (profile ``live``, no injected builder) against the example
overlay copied into a temporary root, with the live factory replaced by a spy, so no store is contacted
and the in-memory builder must stay untouched.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.handlers.floor_fill_scheduled_tick import (
    FloorFillStoreDsnUnsetError,
    run_floor_fill_tick,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    ModelFloorFillCommandRequest,
    ModelFloorFillConfig,
    ModelFloorFillEntryResult,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    ModelFloorFillLiveSettings,
)

pytestmark = pytest.mark.unit

EXAMPLE = Path(__file__).resolve().parents[1] / "overlay.example.yaml"
FILL_OVERLAY_ID = "node_floor_fill_orchestrator"
DSN_VARIABLE = "EXAMPLE_FLOOR_FILL_DSN"
DSN = "postgresql://x"


def _environ(root: Path, *, with_dsn: bool) -> dict[str, str]:
    """The example overlay as ``<root>/<fill overlay id>/overlay.yaml`` plus the roots variable."""
    node = root / FILL_OVERLAY_ID
    node.mkdir(parents=True)
    (node / "overlay.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    env = {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}
    if with_dsn:
        env[DSN_VARIABLE] = DSN
    return env


class _Answers:
    """A fill handler that answers every command with one line and exit code 0."""

    def handle(
        self, request: ModelFloorFillCommandRequest
    ) -> ModelFloorFillEntryResult:
        return ModelFloorFillEntryResult(exit_code=0, stdout="ok line\n")


def _refuse_inmemory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the in-memory builder to a refusal, so a test fails loudly if the tick picks it."""

    def refuse(config: ModelFloorFillConfig) -> _Answers:
        raise AssertionError("in-memory profile bound")

    monkeypatch.setattr(
        "omnimarket.handlers.floor_fill_scheduled_tick.build_inmemory_handler", refuse
    )


def test_live_profile_binds_the_live_factory_and_never_the_inmemory_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[ModelFloorFillLiveSettings, str]] = []

    def factory(
        settings: ModelFloorFillLiveSettings, *, dsn: str
    ) -> Callable[[ModelFloorFillConfig], _Answers]:
        seen.append((settings, dsn))

        def build(config: ModelFloorFillConfig) -> _Answers:
            return _Answers()

        return build

    monkeypatch.setattr(
        "omnimarket.nodes.node_floor_fill_orchestrator.container.container_live"
        ".build_live_handler_factory",
        factory,
    )
    _refuse_inmemory(monkeypatch)
    outcome = run_floor_fill_tick(
        fill_overlay_id=FILL_OVERLAY_ID,
        profile="live",
        environ=_environ(tmp_path, with_dsn=True),
    )
    assert (outcome.exit_code, outcome.first_line) == (0, "ok line")
    assert len(seen) == 1
    live_settings, dsn = seen[0]
    assert dsn == DSN
    assert live_settings.store_dsn_env == DSN_VARIABLE


def test_error_live_profile_without_its_store_dsn_refuses_naming_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_inmemory(monkeypatch)
    with pytest.raises(FloorFillStoreDsnUnsetError) as refused:
        run_floor_fill_tick(
            fill_overlay_id=FILL_OVERLAY_ID,
            profile="live",
            environ=_environ(tmp_path, with_dsn=False),
        )
    assert refused.value.variable == DSN_VARIABLE
