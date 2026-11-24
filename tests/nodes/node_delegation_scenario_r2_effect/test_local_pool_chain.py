# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scenario R2 reaches its local models through the lab's local tier, not one pinned address.

On 2026-11-24 the first local server was the setting ``LLM_CODER_URL``, whose host no longer serves the model (the model
moved to another lab host and is never moved back), so the first ``add local`` was refused and R2 read red every hour
while a local model answered elsewhere. The control below is that incident: the dead first pin with no pool reads red.
The rest show the fix: the run adds the lab servers that answer, least busy first, and the dead-URL refusal and the local
legs assert what they always did.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from omnimarket.handlers.delegation_scenario_runtime import InMemoryScenarioEventSink
from omnimarket.models.delegation.model_endpoint_load import ModelEndpointLoad
from omnimarket.models.delegation.model_endpoint_load_snapshot import (
    ModelEndpointLoadSnapshot,
)
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    ModelScenarioR2RunCommand,
)
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers.handler_lab_local_pool import (
    HandlerLabLocalPool,
)
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers.handler_scenario_r2 import (
    HandlerScenarioR2,
    ModelCommandOutcome,
    ModelLocalServer,
    ModelLocalServerPool,
)
from tests.nodes.node_delegation_scenario_r2_effect.test_golden_chain_scenario_r2_effect import (
    NOW,
    RUNTIME,
    SETTINGS,
    ScriptedOnex,
)

pytestmark = pytest.mark.unit

GONE_URL = "http://gone.invalid:8000"
GONE_MODEL = "Qwen3.8-27B"
BUSY = ModelLocalServer(base_url="http://busy.invalid:8000", model_id="model-busy")
IDLE = ModelLocalServer(base_url="http://idle.invalid:8000", model_id="model-idle")
LIVE_PIN = ModelLocalServer(base_url="http://pc2.invalid:8000", model_id=GONE_MODEL)

#: The first local setting names the host the model left; the second names a host that serves it.
PINNED = {
    **SETTINGS,
    RUNTIME.local_first_env: f"{GONE_URL}|{GONE_MODEL}",
    RUNTIME.local_second_env: f"{LIVE_PIN.base_url}|{LIVE_PIN.model_id}",
}


class RefusingOnex(ScriptedOnex):
    """The product's ``add local``: a URL that does not answer is refused and writes no route."""

    def __init__(self, live: Sequence[str]) -> None:
        super().__init__()
        self.live = tuple(live)
        self.added_urls: list[str] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None,
        home: Path,
        timeout_seconds: int,
    ) -> ModelCommandOutcome:
        args = list(argv)[1:]
        if args[:3] == ["models", "add", "local"]:
            url = args[args.index("--url") + 1]
            if url not in self.live:
                return ModelCommandOutcome(
                    returncode=1, stderr="the server did not answer"
                )
            self.added_urls.append(url)
            # The scripted product answers the dead-URL leg itself; a live URL is a plain add.
            model = args[args.index("--model") + 1]
            self.local.append(model)
            return ModelCommandOutcome(returncode=0, stdout="{}")
        return super().run(
            argv, stdin=stdin, home=home, timeout_seconds=timeout_seconds
        )


class FixedPool:
    def __init__(self, servers: Sequence[ModelLocalServer]) -> None:
        self._servers = tuple(servers)

    def live_servers(self) -> tuple[ModelLocalServer, ...]:
        return self._servers


def _run(
    tmp_path: Path, onex: ScriptedOnex, pool: FixedPool | None
) -> tuple[EnumScenarioConclusion, tuple[str, ...], str]:
    handler = HandlerScenarioR2(
        RUNTIME,
        home=tmp_path,
        environment=PINNED,
        runner=onex,
        appender=[].append,
        sink=InMemoryScenarioEventSink(),
        clock=lambda: NOW,
        pool=pool,
    )
    result = handler.handle(ModelScenarioR2RunCommand())
    return result.conclusion, result.assertions, result.detail


def test_dead_first_pin_with_no_pool_reads_red(tmp_path: Path) -> None:
    """The incident: the first pinned server is gone, so the first add is refused and nothing is listed."""
    onex = RefusingOnex(live=[LIVE_PIN.base_url])
    conclusion, assertions, _ = _run(tmp_path, onex, pool=None)
    assert conclusion is EnumScenarioConclusion.FAILURE
    assert "-local_added" in assertions
    assert "-list_names_both" in assertions


def test_dead_first_pin_reads_green_through_the_lab_pool(tmp_path: Path) -> None:
    """The same dead pin reads green when two lab servers answer: the run adds them, least busy first."""
    onex = RefusingOnex(live=[IDLE.base_url, BUSY.base_url])
    conclusion, assertions, detail = _run(tmp_path, onex, FixedPool([IDLE, BUSY]))
    assert conclusion is EnumScenarioConclusion.SUCCESS, detail
    assert onex.added_urls == [IDLE.base_url, BUSY.base_url]
    assert onex.local == [IDLE.model_id, BUSY.model_id]
    assert GONE_URL not in onex.added_urls
    # The refusal leg and the local legs all still assert, none removed.
    assert {
        "+dead_url_refused",
        "+dead_url_wrote_no_route",
        "+local_added",
        "+list_names_both",
        "+delegated_to_local_url",
        "+both_listed_in_order",
    } <= set(assertions)


def test_one_live_server_fills_the_first_slot_and_the_second_stays_honest(
    tmp_path: Path,
) -> None:
    """With one server answering, the run adds it first; the second slot keeps a pin, so the order assertion says why."""
    onex = RefusingOnex(live=[IDLE.base_url])
    conclusion, assertions, detail = _run(tmp_path, onex, FixedPool([IDLE]))
    assert conclusion is EnumScenarioConclusion.FAILURE
    assert "+local_added" in assertions
    assert "+delegated_to_local_url" in assertions
    assert "-both_listed_in_order" in assertions
    assert "lab pool (one server answers)" in detail


def test_an_empty_pool_leaves_the_pinned_settings_in_force(tmp_path: Path) -> None:
    """A deployment that declares no spread group has an empty pool; the settings stand as they were."""
    onex = RefusingOnex(live=[GONE_URL, LIVE_PIN.base_url])
    conclusion, _, detail = _run(tmp_path, onex, FixedPool([]))
    assert conclusion is EnumScenarioConclusion.SUCCESS, detail
    assert onex.added_urls == [GONE_URL, LIVE_PIN.base_url]


def _pool(
    refs: Mapping[str, str],
    waiting: Mapping[str, int | None],
    lists: Mapping[str, tuple[str, ...] | None],
) -> HandlerLabLocalPool:
    def load(targets: Mapping[str, str]) -> ModelEndpointLoadSnapshot:
        return ModelEndpointLoadSnapshot(
            as_of=NOW,
            loads=tuple(
                ModelEndpointLoad(backend_ref=ref, waiting=waiting.get(ref))
                for ref in targets
            ),
        )

    return HandlerLabLocalPool(
        targets=lambda: refs, load=load, lister=lambda base: lists.get(base)
    )


def test_pool_orders_by_waiting_requests_and_drops_a_server_that_does_not_answer() -> (
    None
):
    refs = {
        "rung-a": "http://a.invalid:8000/v1/chat/completions",
        "rung-b": "http://b.invalid:8000/v1/chat/completions",
        "rung-c": "http://c.invalid:8000/v1/chat/completions",
        "rung-d": "http://d.invalid:8000/v1/chat/completions",
    }
    pool = _pool(
        refs,
        waiting={"rung-a": 5, "rung-b": 1, "rung-c": 0, "rung-d": None},
        lists={
            "http://a.invalid:8000": ("model-a",),
            "http://b.invalid:8000": ("model-b", "other"),
            "http://c.invalid:8000": None,  # refused: does not answer
            "http://d.invalid:8000": ("model-d",),
        },
    )
    assert pool.live_servers() == (
        ModelLocalServer(base_url="http://b.invalid:8000", model_id="model-b"),
        ModelLocalServer(base_url="http://a.invalid:8000", model_id="model-a"),
        ModelLocalServer(base_url="http://d.invalid:8000", model_id="model-d"),
    )


def test_pool_names_one_server_once_when_two_rungs_share_it() -> None:
    refs = {
        "rung-a": "http://a.invalid:8000/v1/chat/completions",
        "rung-b": "http://a.invalid:8000/v1/chat/completions",
    }
    pool = _pool(
        refs,
        waiting={"rung-a": 0, "rung-b": 0},
        lists={"http://a.invalid:8000": ("model-a",)},
    )
    assert [s.base_url for s in pool.live_servers()] == ["http://a.invalid:8000"]


def test_pool_with_no_spread_group_is_empty() -> None:
    assert _pool({}, {}, {}).live_servers() == ()


def test_pool_dispatch_entrypoint_returns_the_live_servers() -> None:
    """The contract binds the pool to the runtime; the runtime dispatches through ``handle``."""
    refs = {"rung-a": "http://a.invalid:8000/v1/chat/completions"}
    pool = _pool(
        refs,
        waiting={"rung-a": 0},
        lists={"http://a.invalid:8000": ("model-a",)},
    )
    result = pool.handle(ModelScenarioR2RunCommand())
    assert result == ModelLocalServerPool(
        servers=(
            ModelLocalServer(base_url="http://a.invalid:8000", model_id="model-a"),
        )
    )
