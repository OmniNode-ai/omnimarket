# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23963: the publish cycle's step limits and cycle bound are the overlay's, never the contract's or the code's.

The contract declares which steps exist and their order. The overlay's ``limits`` carries each step's limit and the cycle bound;
the handler resolves them and refuses, naming the overlay key, when one is missing.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    NODE_DIR,
    HandlerFactoryFloorPublish,
    PublishLimitBoundError,
    PublishLimitMissingError,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
    ModelFactoryFloorPublishRequest,
    ModelPublishLimits,
)
from tests.nodes.node_factory_floor_publish_effect.limits_support import TEST_LIMITS
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    PLUGIN,
    LocalSsh,
    _home,
    _overlay,
)

pytestmark = pytest.mark.unit

MEASURED_MAX_SECONDS = 23
LIMIT_MARGIN = 1.5
RECORD_MODULE = "omnimarket.nodes.node_factory_floor_effect"


def _limits(**changes: Any) -> ModelPublishLimits:
    document: dict[str, Any] = {
        "cycle_bound_seconds": TEST_LIMITS.cycle_bound_seconds,
        "step_timeout_seconds": dict(TEST_LIMITS.step_timeout_seconds),
    }
    document["step_timeout_seconds"].update(changes.pop("steps", {}))
    for name in changes.pop("drop", ()):
        del document["step_timeout_seconds"][name]
    document.update(changes)
    return ModelPublishLimits.model_validate(document)


class SpySsh(LocalSsh):
    """LocalSsh that keeps the time limit each program was started with."""

    def __init__(self) -> None:
        super().__init__()
        self.limits: list[tuple[list[str], int]] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        self.limits.append((list(argv), timeout))
        return super().__call__(argv, stdin, timeout)


def _handle(
    tmp_path: Path, runner: LocalSsh, limits: ModelPublishLimits
) -> dict[str, Any]:
    state = tmp_path / "state"
    state.mkdir()
    overlay = _overlay(tmp_path).model_copy(update={"limits": limits})
    request = ModelFactoryFloorPublishRequest(
        home=str(_home(tmp_path)), overlay=overlay, state_dir=str(state)
    )
    HandlerFactoryFloorPublish(runner).handle(request, environ=PLUGIN)
    served = yaml.safe_load(
        (tmp_path / "site" / "publish-status.json").read_text(encoding="utf-8")
    )
    assert isinstance(served, dict)
    return served


def test_the_contract_declares_no_step_limit_and_no_cycle_bound() -> None:
    """The failing control: on the base commit the contract still states every limit."""
    runtime = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))[
        "runtime"
    ]
    stated = [
        step["name"]
        for step in (*runtime["steps"], *runtime["after_publish_steps"])
        if "timeout_seconds" in step
    ]
    assert stated == []
    assert "bound_seconds" not in runtime["cycle"]


def test_an_unresolved_runtime_refuses_to_read_a_limit() -> None:
    rt = load_runtime()
    with pytest.raises(
        PublishLimitMissingError, match=r"limits\.step_timeout_seconds\.hosts"
    ):
        rt.declared_longest_path()
    with pytest.raises(PublishLimitMissingError, match=r"limits\.cycle_bound_seconds"):
        rt.bound()


def test_the_record_step_limit_and_the_longest_path_fit_the_overlay_bound() -> None:
    rt = load_runtime().with_limits(TEST_LIMITS)
    step = next(s for s in rt.steps if s.name == rt.record_step)
    assert step.required
    assert step.seconds >= MEASURED_MAX_SECONDS * LIMIT_MARGIN
    assert sum(rt.declared_longest_path().values()) <= rt.bound()


def test_the_handler_runs_each_step_with_the_overlay_limit_and_serves_the_overlay_bound(
    tmp_path: Path,
) -> None:
    ssh = SpySsh()
    served = _handle(
        tmp_path,
        ssh,
        _limits(steps={"factory_floor": 77}, cycle_bound_seconds=300),
    )
    record = [limit for argv, limit in ssh.limits if RECORD_MODULE in argv]
    assert record == [77]
    assert served["cycle_bound_seconds"] == 300


def test_the_same_run_with_the_test_limits_gives_the_record_step_45_seconds(
    tmp_path: Path,
) -> None:
    ssh = SpySsh()
    served = _handle(tmp_path, ssh, TEST_LIMITS)
    assert [limit for argv, limit in ssh.limits if RECORD_MODULE in argv] == [45]
    assert served["cycle_bound_seconds"] == 255


def test_a_missing_step_limit_refuses_naming_the_key_and_runs_nothing(
    tmp_path: Path,
) -> None:
    ssh = SpySsh()
    with pytest.raises(
        PublishLimitMissingError, match=r"limits\.step_timeout_seconds\.factory_floor"
    ):
        _handle(tmp_path, ssh, _limits(drop=["factory_floor"]))
    assert ssh.calls == []


def test_a_missing_limit_for_an_after_publish_step_refuses_naming_the_key(
    tmp_path: Path,
) -> None:
    ssh = SpySsh()
    with pytest.raises(
        PublishLimitMissingError, match=r"limits\.step_timeout_seconds\.floor_canary"
    ):
        _handle(tmp_path, ssh, _limits(drop=["floor_canary"]))
    assert ssh.calls == []


def test_an_overlay_without_limits_is_refused_naming_limits() -> None:
    document = _overlay(Path("/tmp/x")).model_dump(mode="json")
    del document["limits"]
    with pytest.raises(ValidationError, match="limits"):
        ModelFactoryFloorPublishOverlay.model_validate(document)


def test_a_limits_document_without_the_bound_is_refused_naming_the_key() -> None:
    with pytest.raises(ValidationError, match="cycle_bound_seconds"):
        ModelPublishLimits.model_validate(
            {"step_timeout_seconds": dict(TEST_LIMITS.step_timeout_seconds)}
        )


def test_an_overlay_limit_for_a_step_the_contract_no_longer_declares_is_ignored() -> (
    None
):
    """Removing a step from the contract never stops the cycle because an old overlay still names it."""
    rt = load_runtime().with_limits(_limits(steps={"retired_step": 5}))
    assert "retired_step" not in {s.name for s in rt.steps}
    assert sum(rt.declared_longest_path().values()) <= rt.bound()


def test_a_bound_below_the_longest_declared_path_refuses_naming_the_bound() -> None:
    longest = sum(
        load_runtime().with_limits(TEST_LIMITS).declared_longest_path().values()
    )
    with pytest.raises(PublishLimitBoundError, match=r"limits\.cycle_bound_seconds"):
        load_runtime().with_limits(_limits(cycle_bound_seconds=longest - 1))
    load_runtime().with_limits(_limits(cycle_bound_seconds=longest))


def test_raising_any_step_past_the_bound_refuses() -> None:
    """The step limit moved to the overlay, so the bound check still catches a raised step."""
    for name in TEST_LIMITS.step_timeout_seconds:
        with pytest.raises(PublishLimitBoundError):
            load_runtime().with_limits(
                _limits(steps={name: TEST_LIMITS.cycle_bound_seconds + 1})
            )


def test_the_handler_source_reads_no_step_limit_from_the_contract_field() -> None:
    source = (NODE_DIR / "handlers" / "handler_factory_floor_publish.py").read_text(
        encoding="utf-8"
    )
    assert "step.timeout_seconds" not in source
    assert "cycle.bound_seconds" not in source
