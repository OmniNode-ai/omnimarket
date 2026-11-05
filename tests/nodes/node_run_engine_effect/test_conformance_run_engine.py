# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Conformance suite of the run engine seam: every backend passes the same cases.

Cases: duplicate start, ordered command steps with no provider, a wait released exactly once, a stale and a duplicate
release, a release for a finished or unknown run, a failing step that stops the run, a double cancel, and a down
backend. The durable backend adds a crash between "step accepted" and "step recorded" and a worker death during a wait.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_run_engine_effect.backends.protocol_run_engine_backend import (
    RunEngineBackendUnavailableError,
)
from omnimarket.nodes.node_run_engine_effect.handlers.handler_run_engine import (
    HandlerRunEngine,
)
from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
    EnumCancelOutcome,
    EnumReleaseOutcome,
    EnumRunEngineOperation,
    EnumRunStatus,
    EnumRunStepKind,
    EnumStartOutcome,
    ModelRunEngineRequest,
    ModelRunSpec,
    ModelRunState,
    ModelRunStepSpec,
)
from tests.nodes.node_run_engine_effect.conftest import BACKENDS, Harness

pytestmark = pytest.mark.unit

SETTLE_SECONDS = 90.0


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def command(name: str, h: Harness, **kw: Any) -> ModelRunStepSpec:
    return ModelRunStepSpec(
        name=name, kind=EnumRunStepKind.COMMAND, argv=h.command(**kw)
    )


def wait(name: str, condition: str) -> ModelRunStepSpec:
    return ModelRunStepSpec(
        name=name, kind=EnumRunStepKind.WAIT_RELEASE, condition=condition
    )


async def start(h: Harness, spec: ModelRunSpec) -> EnumStartOutcome:
    result = await h.handler.handle(
        ModelRunEngineRequest(operation=EnumRunEngineOperation.START_RUN, run=spec)
    )
    assert result.start_outcome is not None
    return result.start_outcome


async def release(
    h: Harness, key: str, condition: str, release_id: str
) -> EnumReleaseOutcome:
    result = await h.handler.handle(
        ModelRunEngineRequest(
            operation=EnumRunEngineOperation.RELEASE_RUN,
            run_key=key,
            condition=condition,
            release_id=release_id,
        )
    )
    assert result.release_outcome is not None
    return result.release_outcome


async def cancel(h: Harness, key: str) -> EnumCancelOutcome:
    result = await h.handler.handle(
        ModelRunEngineRequest(operation=EnumRunEngineOperation.CANCEL_RUN, run_key=key)
    )
    assert result.cancel_outcome is not None
    return result.cancel_outcome


async def state(h: Harness, key: str) -> ModelRunState:
    return await h.backend.get_run_state(key)


async def until_rows(
    h: Harness, key: str, step: str, count: int, seconds: float = 40.0
) -> None:
    deadline = time.monotonic() + seconds
    while h.rows(key, step) < count:
        assert time.monotonic() < deadline, f"{step} never reached {count} rows"
        await asyncio.sleep(0.1)


async def finish(
    h: Harness, key: str, seconds: float = SETTLE_SECONDS
) -> ModelRunState:
    """Poll until the run ends; ``settle`` also returns on a wait, which is the wrong stop after a release."""
    terminal = (EnumRunStatus.COMPLETED, EnumRunStatus.FAILED, EnumRunStatus.CANCELLED)
    deadline = time.monotonic() + seconds
    current = await state(h, key)
    while current.status not in terminal and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        current = await state(h, key)
    return current


def names(s: ModelRunState) -> list[str]:
    return [r.name for r in s.recorded]


def test_duplicate_start_returns_duplicate_and_changes_nothing(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        key = harness.key("dup")
        spec = ModelRunSpec(
            run_key=key, steps=(command("a", harness), command("b", harness))
        )
        assert await start(harness, spec) is EnumStartOutcome.ACCEPTED
        assert await start(harness, spec) is EnumStartOutcome.DUPLICATE
        done = await finish(harness, key)
        assert done.status is EnumRunStatus.COMPLETED
        assert (
            await start(harness, spec) is EnumStartOutcome.DUPLICATE
        )  # a finished run key is not reusable
        assert (harness.rows(key, "a"), harness.rows(key, "b")) == (1, 1)

    run(scenario())


def test_command_steps_run_in_order_with_no_provider(harness: Harness) -> None:
    async def scenario() -> None:
        key = harness.key("order")
        steps = tuple(
            command(n, harness) for n in ("read_state", "pick_next", "record_stop")
        )
        assert (
            await start(harness, ModelRunSpec(run_key=key, steps=steps))
            is EnumStartOutcome.ACCEPTED
        )
        done = await finish(harness, key)
        assert done.status is EnumRunStatus.COMPLETED
        assert names(done) == ["read_state", "pick_next", "record_stop"]
        assert [r.output for r in done.recorded] == ["recorded"] * 3
        assert done.total_steps == 3
        assert done.waiting_on is None

    run(scenario())


def test_wait_releases_exactly_one_next_step(harness: Harness) -> None:
    async def scenario() -> None:
        key = harness.key("wait")
        steps = (
            command("before", harness),
            wait("hold", "merge_window"),
            command("after", harness),
        )
        await start(harness, ModelRunSpec(run_key=key, steps=steps))
        waiting = await harness.backend.settle(key, SETTLE_SECONDS)
        assert waiting.status is EnumRunStatus.WAITING
        assert waiting.waiting_on == "merge_window"
        assert names(waiting) == ["before"]
        assert harness.rows(key, "after") == 0
        assert (
            await release(harness, key, "merge_window", "rel-1")
            is EnumReleaseOutcome.ACCEPTED
        )
        done = await finish(harness, key)
        assert done.status is EnumRunStatus.COMPLETED
        assert names(done) == ["before", "hold", "after"]
        assert done.recorded[1].released_by == "rel-1"
        assert (
            await release(harness, key, "merge_window", "rel-1")
            is EnumReleaseOutcome.DUPLICATE
        )
        assert (
            await release(harness, key, "merge_window", "rel-2")
            is EnumReleaseOutcome.STALE
        )
        await asyncio.sleep(1.0)
        assert harness.rows(key, "after") == 1

    run(scenario())


def test_release_for_a_later_wait_is_stale_and_unknown_run_is_named(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        key = harness.key("stale")
        steps = (
            wait("first", "cond_one"),
            command("mid", harness),
            wait("second", "cond_two"),
        )
        await start(harness, ModelRunSpec(run_key=key, steps=steps))
        await harness.backend.settle(key, SETTLE_SECONDS)
        assert (
            await release(harness, key, "cond_two", "early") is EnumReleaseOutcome.STALE
        )
        assert harness.rows(key, "mid") == 0
        assert (
            await release(harness, harness.key("nobody"), "cond_one", "x")
            is EnumReleaseOutcome.UNKNOWN_RUN
        )
        assert (
            await state(harness, harness.key("nobody"))
        ).status is EnumRunStatus.NOT_FOUND

    run(scenario())


def test_release_of_a_finished_run_is_run_finished(harness: Harness) -> None:
    async def scenario() -> None:
        key = harness.key("fin")
        await start(
            harness, ModelRunSpec(run_key=key, steps=(command("only", harness),))
        )
        await finish(harness, key)
        assert (
            await release(harness, key, "any_cond", "late")
            is EnumReleaseOutcome.RUN_FINISHED
        )

    run(scenario())


def test_a_failing_step_stops_the_run_and_nothing_runs_after_it(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        key = harness.key("fail")
        steps = (
            command("ok", harness),
            command("bad", harness, exit_code=3),
            command("after", harness),
        )
        await start(harness, ModelRunSpec(run_key=key, steps=steps))
        failed = await finish(harness, key)
        assert failed.status is EnumRunStatus.FAILED
        assert names(failed) == ["ok"]
        await asyncio.sleep(1.0)
        assert harness.rows(key, "after") == 0

    run(scenario())


def test_double_cancel_and_release_after_cancel(harness: Harness) -> None:
    async def scenario() -> None:
        key = harness.key("cancel")
        await start(
            harness,
            ModelRunSpec(
                run_key=key, steps=(wait("hold", "cond"), command("never", harness))
            ),
        )
        await harness.backend.settle(key, SETTLE_SECONDS)
        assert await cancel(harness, key) is EnumCancelOutcome.ACCEPTED
        deadline = time.monotonic() + 30
        while (await state(harness, key)).status is not EnumRunStatus.CANCELLED:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.2)
        assert await cancel(harness, key) is EnumCancelOutcome.DUPLICATE
        assert (
            await cancel(harness, harness.key("nobody"))
            is EnumCancelOutcome.UNKNOWN_RUN
        )
        assert (
            await release(harness, key, "cond", "r") is EnumReleaseOutcome.RUN_FINISHED
        )
        assert harness.rows(key, "never") == 0

    run(scenario())


def test_a_down_backend_answers_unknown_never_an_exception() -> None:
    class DownBackend:
        name = "down"
        durable = False

        async def _fail(self, *args: Any) -> Any:
            raise RunEngineBackendUnavailableError("down")

        start_run = release_run = cancel_run = get_run_state = settle = health = _fail

    async def scenario() -> None:
        handler = HandlerRunEngine(DownBackend())
        spec = ModelRunSpec(
            run_key="k1",
            steps=(
                ModelRunStepSpec(
                    name="a", kind=EnumRunStepKind.COMMAND, argv=("true",)
                ),
            ),
        )
        started = await handler.handle(
            ModelRunEngineRequest(operation=EnumRunEngineOperation.START_RUN, run=spec)
        )
        assert started.start_outcome is EnumStartOutcome.UNKNOWN
        released = await handler.handle(
            ModelRunEngineRequest(
                operation=EnumRunEngineOperation.RELEASE_RUN,
                run_key="k1",
                condition="c",
                release_id="r",
            )
        )
        assert released.release_outcome is EnumReleaseOutcome.UNKNOWN
        cancelled = await handler.handle(
            ModelRunEngineRequest(
                operation=EnumRunEngineOperation.CANCEL_RUN, run_key="k1"
            )
        )
        assert cancelled.cancel_outcome is EnumCancelOutcome.UNKNOWN
        health = await handler.handle(
            ModelRunEngineRequest(operation=EnumRunEngineOperation.HEALTH)
        )
        assert health.healthy is False

    run(scenario())


def test_in_memory_run_does_not_survive_a_new_engine_process() -> None:
    from omnimarket.nodes.node_run_engine_effect.backends.backend_in_memory import (
        BackendInMemory,
    )
    from omnimarket.nodes.node_run_engine_effect.backends.step_runner import (
        SubprocessStepRunner,
    )

    async def scenario() -> None:
        first = BackendInMemory(SubprocessStepRunner())
        assert first.durable is False
        spec = ModelRunSpec(run_key="gone", steps=(wait("hold", "c"),))
        assert await first.start_run(spec) is EnumStartOutcome.ACCEPTED
        second = BackendInMemory(SubprocessStepRunner())
        assert (await second.get_run_state("gone")).status is EnumRunStatus.NOT_FOUND

    run(scenario())


if "temporal_self_hosted" in BACKENDS:
    from tests.nodes.node_run_engine_effect.conftest import TEMPORAL_OVERLAY_ENV

    def test_kill_between_step_accepted_and_recorded_keeps_one_row_for_a_keyed_command(
        durable: Harness,
    ) -> None:
        async def scenario() -> None:
            h = durable
            key = h.key("crash")
            steps = (
                command("s1", h),
                command("effect", h, idem=1, after=6.0),
                command("done", h),
            )
            assert (
                await start(h, ModelRunSpec(run_key=key, steps=steps))
                is EnumStartOutcome.ACCEPTED
            )
            await until_rows(
                h, key, "effect", 1
            )  # the effect row is written, the step is not yet recorded
            h.kill_workers()
            mid = await state(h, key)
            assert mid.status is EnumRunStatus.RUNNING
            assert "effect" not in names(mid)
            h.start_worker()
            done = await finish(h, key)
            assert done.status is EnumRunStatus.COMPLETED
            assert names(done) == ["s1", "effect", "done"]
            await asyncio.sleep(1.0)
            assert (h.rows(key, "s1"), h.rows(key, "effect"), h.rows(key, "done")) == (
                1,
                1,
                1,
            )

        run(scenario())

    def test_an_unkeyed_command_repeats_after_the_same_kill(durable: Harness) -> None:
        """Characterises the engine: delivery is at least once, so the command must carry the key."""

        async def scenario() -> None:
            h = durable
            key = h.key("unkeyed")
            steps = (command("effect", h, idem=0, after=6.0),)
            await start(h, ModelRunSpec(run_key=key, steps=steps))
            await until_rows(h, key, "effect", 1)
            h.kill_workers()
            h.start_worker()
            done = await finish(h, key)
            assert done.status is EnumRunStatus.COMPLETED
            assert h.rows(key, "effect") == 2

        run(scenario())

    def test_worker_death_during_a_wait_loses_nothing_and_a_release_yields_one_step(
        durable: Harness,
    ) -> None:
        async def scenario() -> None:
            h = durable
            key = h.key("waitkill")
            steps = (command("before", h), wait("hold", "gate"), command("after", h))
            await start(h, ModelRunSpec(run_key=key, steps=steps))
            assert (
                await h.backend.settle(key, SETTLE_SECONDS)
            ).status is EnumRunStatus.WAITING
            h.kill_workers()
            still = await state(h, key)
            assert still.status is EnumRunStatus.WAITING
            assert names(still) == ["before"]
            assert (
                await release(h, key, "gate", "r1") is EnumReleaseOutcome.ACCEPTED
            )  # accepted with no worker running
            assert h.rows(key, "after") == 0
            h.start_worker()
            done = await finish(h, key)
            assert done.status is EnumRunStatus.COMPLETED
            await asyncio.sleep(1.0)
            assert h.rows(key, "after") == 1

        run(scenario())

    def test_a_temporal_server_that_is_not_there_answers_unknown() -> None:
        import os

        import yaml

        from omnimarket.nodes.node_run_engine_effect.backends.backend_temporal_self_hosted import (
            BackendTemporalSelfHosted,
        )
        from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
            ModelRunEngineOverlay,
        )

        document = yaml.safe_load(Path(os.environ[TEMPORAL_OVERLAY_ENV]).read_text())
        document["temporal_self_hosted"]["target_host"] = "127.0.0.1:1"
        settings = ModelRunEngineOverlay.model_validate(document).temporal_self_hosted
        assert settings is not None

        async def scenario() -> None:
            handler = HandlerRunEngine(BackendTemporalSelfHosted(settings))
            spec = ModelRunSpec(
                run_key="down-1",
                steps=(
                    ModelRunStepSpec(
                        name="a", kind=EnumRunStepKind.COMMAND, argv=("true",)
                    ),
                ),
            )
            result = await handler.handle(
                ModelRunEngineRequest(
                    operation=EnumRunEngineOperation.START_RUN, run=spec
                )
            )
            assert result.start_outcome is EnumStartOutcome.UNKNOWN
            health = await handler.handle(
                ModelRunEngineRequest(operation=EnumRunEngineOperation.HEALTH)
            )
            assert health.healthy is False

        run(scenario())

    def test_a_server_that_vanishes_after_connecting_answers_unknown_not_an_exception() -> (
        None
    ):
        """A cached client whose server died raises an RPC error, not a connect error; every operation fails closed."""
        import os

        import yaml
        from temporalio.service import RPCError, RPCStatusCode

        from omnimarket.nodes.node_run_engine_effect.backends.backend_temporal_self_hosted import (
            BackendTemporalSelfHosted,
        )
        from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
            ModelRunEngineOverlay,
        )

        document = yaml.safe_load(Path(os.environ[TEMPORAL_OVERLAY_ENV]).read_text())
        settings = ModelRunEngineOverlay.model_validate(document).temporal_self_hosted
        assert settings is not None

        def gone(*_args: Any, **_kwargs: Any) -> Any:
            raise RPCError("gone", RPCStatusCode.UNAVAILABLE, b"")

        class Handle:
            def fetch_history_events(self) -> Any:
                raise RPCError("gone", RPCStatusCode.UNAVAILABLE, b"")

            async def signal(self, *_a: Any, **_k: Any) -> None:
                gone()

            async def cancel(self, *_a: Any, **_k: Any) -> None:
                gone()

        class Service:
            async def check_health(self) -> None:
                gone()

        class Client:
            service_client = Service()

            async def start_workflow(self, *_a: Any, **_k: Any) -> None:
                gone()

            def get_workflow_handle(self, _id: str) -> Handle:
                return Handle()

        async def scenario() -> None:
            backend = BackendTemporalSelfHosted(settings)
            backend._client = Client()
            handler = HandlerRunEngine(backend)
            spec = ModelRunSpec(
                run_key="vanish-1",
                steps=(
                    ModelRunStepSpec(
                        name="a", kind=EnumRunStepKind.COMMAND, argv=("true",)
                    ),
                ),
            )
            started = await handler.handle(
                ModelRunEngineRequest(
                    operation=EnumRunEngineOperation.START_RUN, run=spec
                )
            )
            assert started.start_outcome is EnumStartOutcome.UNKNOWN
            backend._client = Client()
            released = await handler.handle(
                ModelRunEngineRequest(
                    operation=EnumRunEngineOperation.RELEASE_RUN,
                    run_key="vanish-1",
                    condition="c",
                    release_id="r",
                )
            )
            assert released.release_outcome is EnumReleaseOutcome.UNKNOWN
            backend._client = Client()
            health = await handler.handle(
                ModelRunEngineRequest(operation=EnumRunEngineOperation.HEALTH)
            )
            assert health.healthy is False

        run(scenario())
